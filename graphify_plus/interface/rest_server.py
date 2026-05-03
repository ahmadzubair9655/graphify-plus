"""12.7 — REST server (`gp serve`).

Bearer-token-authenticated FastAPI app exposing the same surface as
the MCP server, plus a thin wrapper for ``gp explain`` and ``gp audit``.
The web UI (`/ui/`) is served only if the optional ``[web]`` extra is
installed.

Endpoints (under ``/v1``):
  - GET  /v1/health
  - POST /v1/context           {repo, target, max_tokens, min_confidence}
  - POST /v1/plan              {repo, task}
  - POST /v1/find              {repo, query, k}
  - POST /v1/simulate          {repo, edits}
  - GET  /v1/explain/{symbol}  ?repo=<path>
  - GET  /v1/audit             ?repo=<path>

Token bytes live at ``<repo>/.graphify_plus/.serve_token``. ``--no-auth``
disables auth (localhost-only dev mode).
"""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Any

try:
    from fastapi import Depends, FastAPI, HTTPException, Request, status
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAPI_AVAILABLE = False

TOKEN_NAME = ".serve_token"
DEFAULT_MAX_TOKENS = 8000


def _token_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / TOKEN_NAME


def generate_token(repo: Path) -> str:
    """Mint a new UUID4 token, persist it under the cache dir."""
    p = _token_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    token = str(uuid.uuid4())
    p.write_text(token)
    p.chmod(0o600)
    return token


def read_token(repo: Path) -> str | None:
    p = _token_path(repo)
    if not p.exists():
        return None
    return p.read_text().strip() or None


def build_app(repo: Path, *, no_auth: bool = False) -> Any:
    if not _FASTAPI_AVAILABLE:
        raise RuntimeError("REST server requires: pip install graphify-plus[serve]")
    expected = None if no_auth else read_token(repo)
    if not no_auth and expected is None:
        raise RuntimeError(
            "No auth token found. Run 'gp serve --gen-token' first, "
            "or pass --no-auth for localhost-only development."
        )

    bearer = HTTPBearer(auto_error=False)
    _bearer_dep = Depends(bearer)

    def _check_auth(creds: HTTPAuthorizationCredentials | None = _bearer_dep) -> None:
        if no_auth:
            return
        if creds is None or not secrets.compare_digest(creds.credentials, expected or ""):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing bearer token",
            )

    app = FastAPI(title="graphify-plus", version="4.5.0")

    @app.get("/v1/health")
    def health() -> dict:
        return {"status": "ok", "repo": str(repo)}

    @app.post("/v1/context")
    async def context(req: Request, _auth: None = Depends(_check_auth)) -> JSONResponse:
        body = await req.json()
        target = body.get("target")
        if not target:
            raise HTTPException(status_code=400, detail="missing 'target'")
        max_tokens = int(body.get("max_tokens") or DEFAULT_MAX_TOKENS)
        min_conf = float(body.get("min_confidence") or 0.0)
        return JSONResponse(
            _run_context(
                Path(body.get("repo") or repo),
                str(target),
                max_tokens=max_tokens,
                min_confidence=min_conf,
            )
        )

    @app.post("/v1/plan")
    async def plan(req: Request, _auth: None = Depends(_check_auth)) -> JSONResponse:
        body = await req.json()
        task = body.get("task")
        if not task:
            raise HTTPException(status_code=400, detail="missing 'task'")
        return JSONResponse(
            _run_plan(
                Path(body.get("repo") or repo),
                str(task),
                force=bool(body.get("force") or False),
            )
        )

    @app.post("/v1/find")
    async def find(req: Request, _auth: None = Depends(_check_auth)) -> JSONResponse:
        body = await req.json()
        query = body.get("query")
        if not query:
            raise HTTPException(status_code=400, detail="missing 'query'")
        k = int(body.get("k") or 10)
        return JSONResponse(_run_find(Path(body.get("repo") or repo), str(query), k=k))

    @app.post("/v1/simulate")
    async def simulate(req: Request, _auth: None = Depends(_check_auth)) -> JSONResponse:
        body = await req.json()
        edits = body.get("edits") or []
        return JSONResponse(_run_simulate(Path(body.get("repo") or repo), list(edits)))

    @app.post("/v1/skeleton")
    async def skeleton(req: Request, _auth: None = Depends(_check_auth)) -> JSONResponse:
        body = await req.json()
        target = body.get("target")
        if not target:
            raise HTTPException(status_code=400, detail="missing 'target'")
        return JSONResponse(_run_skeleton(Path(body.get("repo") or repo), str(target)))

    @app.get("/v1/explain/{symbol}")
    def explain(symbol: str, _auth: None = Depends(_check_auth)) -> JSONResponse:
        return JSONResponse(_run_explain(repo, symbol))

    @app.get("/v1/audit")
    def audit(_auth: None = Depends(_check_auth)) -> JSONResponse:
        return JSONResponse(_run_audit(repo))

    @app.get("/ui/", response_class=HTMLResponse)
    def ui() -> HTMLResponse:
        try:
            from .web_ui import render_index

            return HTMLResponse(render_index())
        except ImportError:
            raise HTTPException(
                status_code=503,
                detail="Web UI requires: pip install graphify-plus[web]",
            ) from None

    return app


# ---------------------------------------------------------------------------
# Thin adapters into existing query/CLI logic
# ---------------------------------------------------------------------------


def _run_context(repo: Path, target: str, *, max_tokens: int, min_confidence: float) -> dict:
    from ..core.symbol_graph import build as build_graph
    from ..query.budget import frame_to_budget
    from ..query.partition import compute_partition
    from ..runtime.store import Store, cache_path

    store = Store(cache_path(repo))
    try:
        if store.get_symbol(target) is not None:
            tid = target
        else:
            matches = store.find_symbols_by_qualified_name(target)
            if not matches:
                return {"error": f"no symbol matches: {target}"}
            tid = matches[0]["id"]
        symbols = store.all_symbols()
        edges = store.all_edges()
        if min_confidence > 0:
            edges = [e for e in edges if float(e.get("confidence", 0.2)) >= min_confidence]
        G = build_graph(symbols, edges)
        # Use target's module as entry.
        target_sym = store.get_symbol(tid)
        if target_sym is None:
            return {"error": "target vanished"}
        same_path = store.symbols_in_path(target_sym.get("path") or "")
        entry_id = next((s["id"] for s in same_path if s.get("kind") == "module"), tid)
        try:
            partition = compute_partition(G, entry_id, tid)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"no path: {exc}"}
        framed = frame_to_budget(partition, store, max_tokens=max_tokens)
        return {
            "target": target,
            "tokens": framed.tokens,
            "max_tokens": max_tokens,
            "text": framed.text,
        }
    finally:
        store.close()


def _run_plan(repo: Path, task: str, *, force: bool = False) -> dict:
    from ..query.sync_docs import regenerate
    from ..runtime.store import cache_path

    if not cache_path(repo).exists():
        return {"error": f"no cache at {cache_path(repo)}"}
    plan_path = regenerate(repo, task, force=force)
    try:
        body = plan_path.read_text(errors="replace")
    except OSError as exc:
        return {"error": f"could not read plan: {exc}"}
    return {
        "repo": str(repo),
        "task": task,
        "plan_path": str(plan_path),
        "plan": body,
    }


def _run_find(repo: Path, query: str, *, k: int) -> dict:
    from ..runtime.store import Store, cache_path

    store = Store(cache_path(repo))
    try:
        matches = store.find_symbols_by_qualified_name(query)[:k]
        return {
            "query": query,
            "matches": [
                {
                    "id": s["id"],
                    "qualified_name": s.get("qualified_name"),
                    "kind": s.get("kind"),
                    "path": s.get("path"),
                }
                for s in matches
            ],
        }
    finally:
        store.close()


def _run_simulate(repo: Path, edits: list) -> dict:
    """``edits`` is a list of dicts: {op: add_edge|remove_edge|add_node|rm_node, ...}.

    add_edge/remove_edge: {op, src, dst, kind?}
    add_node:             {op, id}
    rm_node:              {op, id}
    """
    from ..core.symbol_graph import build as build_graph
    from ..query.shadow import simulate as shadow_simulate
    from ..runtime.overlay import empty as empty_overlay
    from ..runtime.rules import RuleSet
    from ..runtime.store import Store, cache_path

    if not cache_path(repo).exists():
        return {"error": f"no cache at {cache_path(repo)}"}
    store = Store(cache_path(repo))
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        overlay = empty_overlay(G)
        for e in edits:
            op = e.get("op")
            if op == "add_node":
                nid = str(e.get("id") or "")
                if nid:
                    overlay.add_node(nid, kind="stub", path="", name=nid)
            elif op == "rm_node":
                nid = str(e.get("id") or "")
                if nid:
                    overlay.remove_node(nid)
            elif op == "add_edge":
                src = str(e.get("src") or "")
                dst = str(e.get("dst") or "")
                kind = str(e.get("kind") or "calls")
                if src and dst:
                    if not G.has_node(dst):
                        overlay.add_node(dst, kind="stub", path="", name=dst)
                    overlay.add_edge(src, dst, kind=kind)
            elif op == "remove_edge":
                src = str(e.get("src") or "")
                dst = str(e.get("dst") or "")
                kind = e.get("kind")
                if src and dst:
                    overlay.remove_edge(src, dst, kind=kind or None)
        rules_path = repo / ".graphify_plus" / "rules.yaml"
        if rules_path.exists():
            try:
                import yaml  # type: ignore[import-untyped]

                ruleset = RuleSet.from_dict(yaml.safe_load(rules_path.read_text()) or {})
            except ImportError:
                import json as _json

                ruleset = RuleSet.from_dict(_json.loads(rules_path.read_text()))
        else:
            ruleset = RuleSet()
        result = shadow_simulate(overlay, ruleset)
    finally:
        store.close()
    return {
        "grade": result.grade,
        "summary": result.summary,
        "violations": [
            {
                "rule_id": v.rule_id,
                "severity": v.severity,
                "src": v.src,
                "dst": v.dst,
                "kind": v.kind,
                "message": v.message,
            }
            for v in result.violations
        ],
    }


def _run_skeleton(repo: Path, target: str) -> dict:
    from ..runtime.store import Store, cache_path

    store = Store(cache_path(repo))
    try:
        if store.get_symbol(target) is not None:
            sid = target
        else:
            matches = store.find_symbols_by_qualified_name(target)
            if not matches:
                return {"error": f"no symbol matches: {target}"}
            sid = matches[0]["id"]
        sym = store.get_symbol(sid)
        return {
            "id": sid,
            "qualified_name": sym.get("qualified_name") if sym else None,
            "skeleton": store.get_skeleton_for_symbol(sid),
        }
    finally:
        store.close()


def _run_explain(repo: Path, symbol: str) -> dict:
    from ..core.symbol_graph import build as build_graph
    from ..runtime.store import Store, cache_path

    store = Store(cache_path(repo))
    try:
        if store.get_symbol(symbol) is not None:
            sid = symbol
        else:
            matches = store.find_symbols_by_qualified_name(symbol)
            if not matches:
                return {"error": f"no symbol matches: {symbol}"}
            sid = matches[0]["id"]
        sym = store.get_symbol(sid)
        symbols = store.all_symbols()
        edges = store.all_edges()
        G = build_graph(symbols, edges)
        out_edges = [
            {
                "to": str(v),
                "kind": data.get("kind"),
                "confidence": data.get("confidence"),
            }
            for _u, v, _k, data in G.out_edges(sid, keys=True, data=True)
        ]
        in_edges = [
            {
                "from": str(u),
                "kind": data.get("kind"),
                "confidence": data.get("confidence"),
            }
            for u, _v, _k, data in G.in_edges(sid, keys=True, data=True)
        ]
        return {
            "id": sid,
            "symbol": sym,
            "skeleton": store.get_skeleton_for_symbol(sid),
            "out_edges": out_edges,
            "in_edges": in_edges,
        }
    finally:
        store.close()


def _run_audit(repo: Path) -> dict:
    from ..audit.probe import run_audit
    from ..core.symbol_graph import build as build_graph
    from ..runtime.store import Store, cache_path

    store = Store(cache_path(repo))
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        return run_audit(G, iterations=2)
    finally:
        store.close()


__all__ = ["build_app", "generate_token", "read_token"]
