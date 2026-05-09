"""Model Context Protocol server.

Exposes Graphify-Plus capabilities as MCP tools so an MCP-aware AI
client (Claude Code, Cursor, etc.) can call them directly without
shelling out to the CLI.

Tools exposed (input schemas mirror the corresponding ``gp`` CLI):

  gp_init, gp_plan, gp_context, gp_skeleton, gp_find, gp_simulate,
  gp_guardrails, gp_audit, gp_diff, gp_blast_radius, gp_coordinate,
  gp_visual

Outputs are token-budgeted to ≤ 8 000 tokens by default. Stdio
transport. Run via ``python -m graphify_plus.interface.mcp_server``
or as a subcommand: ``graphify-plus mcp``.

The ``mcp`` SDK is an optional dependency installed via the ``[mcp]``
extra. We import it lazily so the module is safe to import without it
(useful for tooling that just inspects the manifest).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.symbol_graph import build as build_graph
from ..daemon import DaemonClient, DaemonNotRunning, InMemoryGraph
from ..daemon.handlers import HANDLERS as INTENT_HANDLERS
from ..daemon.receipts import make_receipt
from ..query.budget import frame_to_budget
from ..query.partition import compute_partition
from ..query.semantic import find as semantic_find
from ..runtime.store import Store, cache_path

# ---------- tool implementations (independent of the MCP SDK) -----------


def _open(repo: Path) -> Store:
    if not cache_path(repo).exists():
        raise RuntimeError(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    return Store(cache_path(repo))


def tool_init(args: dict[str, Any]) -> dict[str, Any]:
    from ..core import cas
    from ..core.ingest import ingest
    from ..core.skeletonizer import skeletonize_all

    repo = Path(args["repo"]).resolve()
    parallel = bool(args.get("parallel", True))
    res = ingest(repo, parallel=parallel)
    skeletons = skeletonize_all(res.symbols)
    store = Store(cache_path(repo))
    try:
        store.replace_all(res.symbols, res.edges)
        store.set_meta("repo_root", str(repo))
        for sid, body in skeletons.items():
            h = cas.put(store, body)
            store.link_skeleton(sid, h)
    finally:
        store.close()
    return {
        "files_parsed": res.files_parsed,
        "files_skipped": res.files_skipped,
        "symbols": len(res.symbols),
        "edges": len(res.edges),
    }


def tool_context(args: dict[str, Any]) -> dict[str, Any]:
    repo = Path(args["repo"]).resolve()
    target = args["target"]
    entry = args.get("entry")
    max_tokens = int(args.get("max_tokens", 8000))
    store = _open(repo)
    try:
        target_id = target if store.get_symbol(target) else None
        if target_id is None:
            matches = store.find_symbols_by_qualified_name(target)
            if not matches:
                raise RuntimeError(f"No symbol matches: {target}")
            if len(matches) > 1:
                raise RuntimeError(
                    f"Ambiguous target {target!r} ({len(matches)} matches) — "
                    "use a fully qualified name."
                )
            target_id = matches[0]["id"]
        if entry:
            entry_syms = store.symbols_in_path(entry)
            if not entry_syms:
                raise RuntimeError(f"No symbols at entry {entry}")
            entry_id = entry_syms[0]["id"]
        else:
            same_path = store.symbols_in_path(store.get_symbol(target_id)["path"] or "")
            entry_id = next(
                (s["id"] for s in same_path if s.get("kind") == "module"),
                target_id,
            )
        G = build_graph(store.all_symbols(), store.all_edges())
        partition = compute_partition(G, entry_id, target_id)
        framed = frame_to_budget(partition, store, max_tokens=max_tokens)
    finally:
        store.close()
    return {
        "text": framed.text,
        "tokens": framed.tokens,
        "manifest_hashes": framed.manifest,
        "truncated": framed.truncated_count,
        "cut": framed.cut_count,
    }


def tool_skeleton(args: dict[str, Any]) -> dict[str, Any]:
    repo = Path(args["repo"]).resolve()
    store = _open(repo)
    try:
        if "file" in args:
            syms = store.symbols_in_path(args["file"])
            chunks = [b for s in syms if (b := store.get_skeleton_for_symbol(s["id"]))]
            return {"text": "\n\n".join(chunks)}
        target = args["target"]
        body = store.get_skeleton_for_symbol(target)
        if body:
            return {"text": body}
        matches = store.find_symbols_by_qualified_name(target)
        if not matches:
            raise RuntimeError(f"No symbol matches: {target}")
        body = store.get_skeleton_for_symbol(matches[0]["id"])
        return {"text": body or ""}
    finally:
        store.close()


def tool_find(args: dict[str, Any]) -> dict[str, Any]:
    repo = Path(args["repo"]).resolve()
    intent = args["intent"]
    top_k = int(args.get("top_k", 8))
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        matches = semantic_find(store, G, intent, top_k=top_k)
    finally:
        store.close()
    return {
        "matches": [
            {
                "qualified_name": m.symbol.get("qualified_name"),
                "kind": m.symbol.get("kind"),
                "path": m.symbol.get("path"),
                "score": round(m.score, 4),
                "community": m.community,
            }
            for m in matches
        ]
    }


def tool_simulate(args: dict[str, Any]) -> dict[str, Any]:
    from ..query.shadow import simulate
    from ..runtime.overlay import empty
    from .cli.safety_cmds import _load_ruleset

    repo = Path(args["repo"]).resolve()
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        overlay = empty(G)
        for spec in args.get("add_edge", []):
            kind = spec.get("kind", "calls")
            overlay.add_edge(spec["src"], spec["dst"], kind=kind)
        for spec in args.get("remove_edge", []):
            overlay.remove_edge(spec["src"], spec["dst"], kind=spec.get("kind"))
        result = simulate(overlay, _load_ruleset(repo))
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


def tool_guardrails(args: dict[str, Any]) -> dict[str, Any]:
    from ..query.guardrails import evaluate_payload
    from .cli.safety_cmds import _load_ruleset

    repo = Path(args["repo"]).resolve()
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        decision = evaluate_payload(store, G, args["payload"], _load_ruleset(repo))
    finally:
        store.close()
    return {
        "block": decision.block,
        "rule_id": decision.rule_id,
        "suggestion": decision.suggestion,
    }


def tool_blast_radius(args: dict[str, Any]) -> dict[str, Any]:
    from ..query.blast_radius import trace as blast_trace

    repo = Path(args["repo"]).resolve()
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        report = blast_trace(G, args["package"])
    finally:
        store.close()
    return {
        "package": report.package,
        "suggestion": report.suggestion,
        "hits": [
            {"qualified_name": h.qualified_name, "path": h.path, "depth": h.depth}
            for h in report.hits
        ],
    }


# ---------- intent-tool wrapper (daemon-backed with direct fallback) -----


def _run_intent(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Route an intent tool through the daemon when available, else execute
    in-process by building the in-memory graph from the on-disk cache.

    Same response envelope either way:
        {ok, freshness, receipt, results, more_available, [extra]}
    """
    repo = Path(args.pop("repo", ".")).resolve()
    handler_args = {k: v for k, v in args.items() if k != "request_id"}

    client = DaemonClient(repo)
    if client.is_running():
        try:
            return client.call(op, handler_args)
        except DaemonNotRunning:
            pass  # fall through to direct path

    # Direct path: build the snapshot from disk. Slower (cold-start the whole
    # daemon for one query), but a useful safety net before users have run
    # `gp daemon start`.
    if op in ("ping", "graph_stats"):
        # graph_stats without daemon is meaningless other than as a probe.
        return {"ok": True, "running": False, "results": [], "more_available": 0}
    handler = INTENT_HANDLERS.get(op)
    if handler is None:
        return {
            "ok": False,
            "error": {"code": "UNKNOWN_OP", "message": f"unknown intent op: {op}"},
        }

    store = Store(cache_path(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()

    import time as _time

    t0 = _time.perf_counter()
    payload = handler(snap, handler_args)
    elapsed_ms = (_time.perf_counter() - t0) * 1000.0
    if "error" in payload:
        return {"ok": False, "error": payload["error"]}
    results = payload.get("results", []) or []
    return {
        "ok": True,
        "freshness": snap.freshness(),
        "receipt": make_receipt(op, elapsed_ms, results, len(results)),
        "results": results,
        "more_available": payload.get("more_available", 0),
        "extra": payload.get("extra", {}),
    }


def tool_whats_in(args: dict[str, Any]) -> dict[str, Any]:
    """List the symbols inside a file or directory, ranked by structural weight."""
    return _run_intent("whats_in", args)


def tool_who_calls(args: dict[str, Any]) -> dict[str, Any]:
    """Direct callers (or referencers) of a symbol."""
    return _run_intent("who_calls", args)


def tool_whos_called_by(args: dict[str, Any]) -> dict[str, Any]:
    """Direct callees of a symbol."""
    return _run_intent("whos_called_by", args)


def tool_what_depends_on(args: dict[str, Any]) -> dict[str, Any]:
    """Inbound-edge dependents (calls, refs, imports, extends, implements)."""
    return _run_intent("what_depends_on", args)


def tool_what_does_this_depend_on(args: dict[str, Any]) -> dict[str, Any]:
    """Outbound dependencies of a symbol."""
    return _run_intent("what_does_this_depend_on", args)


def tool_find_by_name(args: dict[str, Any]) -> dict[str, Any]:
    """Fuzzy label match. Returns confidence-ranked candidates."""
    return _run_intent("find_by_name", args)


def tool_find_by_concept(args: dict[str, Any]) -> dict[str, Any]:
    """Concept / natural-language search over the symbol graph."""
    return _run_intent("find_by_concept", args)


def tool_whats_central(args: dict[str, Any]) -> dict[str, Any]:
    """Top-N central symbols by PageRank."""
    return _run_intent("whats_central", args)


def tool_plan(args: dict[str, Any]) -> dict[str, Any]:
    """Graph-grounded edit plan for a natural-language task description."""
    return _run_intent("plan", args)


def tool_whats_untested(args: dict[str, Any]) -> dict[str, Any]:
    """Symbols at or below max_pct test coverage in the requested scope."""
    return _run_intent("whats_untested", args)


def tool_coverage_for(args: dict[str, Any]) -> dict[str, Any]:
    """Coverage stats for a single symbol."""
    return _run_intent("coverage_for", args)


def tool_coverage_summary(args: dict[str, Any]) -> dict[str, Any]:
    """Repo-wide test-coverage roll-up + worst-N files."""
    return _run_intent("coverage_summary", args)


def tool_rules_check(args: dict[str, Any]) -> dict[str, Any]:
    """Run architectural-drift rules from .graphify_plus/rules.yaml."""
    return _run_intent("rules_check", args)


def tool_review(args: dict[str, Any]) -> dict[str, Any]:
    """PR review co-pilot — touched/central/untested + rules + blast radius."""
    return _run_intent("review", args)


def tool_onboard(args: dict[str, Any]) -> dict[str, Any]:
    """Onboarding walkthrough — central nodes + per-module exemplars."""
    return _run_intent("onboard", args)


# ---------- registry ----------------------------------------------------


TOOLS: dict[str, Callable[[dict], dict]] = {
    "gp_init": tool_init,
    "gp_context": tool_context,
    "gp_skeleton": tool_skeleton,
    "gp_find": tool_find,
    "gp_simulate": tool_simulate,
    "gp_guardrails": tool_guardrails,
    "gp_blast_radius": tool_blast_radius,
    # Sprint 2 intent-typed tools (daemon-backed):
    "gp_whats_in": tool_whats_in,
    "gp_who_calls": tool_who_calls,
    "gp_whos_called_by": tool_whos_called_by,
    "gp_what_depends_on": tool_what_depends_on,
    "gp_what_does_this_depend_on": tool_what_does_this_depend_on,
    "gp_find_by_name": tool_find_by_name,
    "gp_find_by_concept": tool_find_by_concept,
    "gp_whats_central": tool_whats_central,
    "gp_plan": tool_plan,
    # Sprint 8 — test-coverage overlay:
    "gp_whats_untested": tool_whats_untested,
    "gp_coverage_for": tool_coverage_for,
    "gp_coverage_summary": tool_coverage_summary,
    # Sprint 9.4 — architectural drift rules:
    "gp_rules_check": tool_rules_check,
    # Sprint 10.1 — PR review co-pilot:
    "gp_review": tool_review,
    # Sprint 10.2 — onboarding walkthrough:
    "gp_onboard": tool_onboard,
}


def manifest() -> dict[str, Any]:
    """Static manifest written to ``<target>/.graphify_plus/mcp.json`` by
    ``gp init`` so MCP clients can autodiscover the tool surface."""
    return {
        "version": 1,
        "transport": "stdio",
        "tools": [
            {
                "name": name,
                "description": (fn.__doc__ or name).strip().splitlines()[0] if fn.__doc__ else name,
            }
            for name, fn in TOOLS.items()
        ],
    }


# ---------- stdio entry point -------------------------------------------


async def _serve_async() -> None:  # pragma: no cover — exercised only in production
    """Run the MCP server over stdio, requires ``mcp`` extra."""
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "MCP SDK not installed. Install with: pip install 'graphify-plus[mcp]'"
        ) from e

    server = Server("graphify-plus")

    @server.list_tools()  # type: ignore[misc]
    async def _list_tools():
        from mcp.types import Tool

        return [
            Tool(
                name=name, description=(fn.__doc__ or name).strip(), inputSchema={"type": "object"}
            )
            for name, fn in TOOLS.items()
        ]

    @server.call_tool()  # type: ignore[misc]
    async def _call_tool(name: str, arguments: dict):
        from mcp.types import TextContent

        if name not in TOOLS:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool {name}"}))]
        try:
            result = TOOLS[name](arguments or {})
        except Exception as e:  # noqa: BLE001
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> int:  # pragma: no cover
    import asyncio

    asyncio.run(_serve_async())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "TOOLS",
    "manifest",
    "tool_init",
    "tool_context",
    "tool_skeleton",
    "tool_find",
    "tool_simulate",
    "tool_guardrails",
    "tool_blast_radius",
    "tool_whats_in",
    "tool_who_calls",
    "tool_whos_called_by",
    "tool_what_depends_on",
    "tool_what_does_this_depend_on",
    "tool_find_by_name",
    "tool_find_by_concept",
    "tool_whats_central",
    "tool_plan",
    "tool_whats_untested",
    "tool_coverage_for",
    "tool_coverage_summary",
    "tool_rules_check",
    "tool_review",
    "tool_onboard",
]
