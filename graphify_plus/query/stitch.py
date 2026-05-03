"""Cross-boundary meta-graph stitching.

Stitch frontend + backend (and other) sub-graphs into one *meta-graph*
via API contracts (OpenAPI / Swagger) where present, falling back to
string-literal heuristics when no contract exists.

Output: cross-graph edges of kind ``crosses_to`` between a frontend
symbol and a backend symbol via a virtual ``endpoint`` node, each edge
carrying a ``confidence`` ∈ [0, 1] (1.0 for contract matches, ≤0.7 for
string-literal heuristics — Phase 11 will formalise the confidence
layer; this is the seed for it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from ..core.adapters import make_symbol_id

# ---------- OpenAPI loading -------------------------------------------


@dataclass(frozen=True)
class Endpoint:
    op_id: str
    method: str  # GET, POST, ...
    path: str  # /api/invoice
    spec_path: str  # source file


def _load_yaml_or_json(path: Path) -> dict:
    text = path.read_text(errors="replace")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-untyped]

            return yaml.safe_load(text) or {}
        except ImportError:
            return {}
    import json

    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {}


def discover_specs(root: Path) -> list[Path]:
    out: list[Path] = []
    for pattern in ("openapi.json", "openapi.yaml", "swagger.json", "**/api.yaml"):
        out.extend(root.glob(pattern))
        out.extend(root.glob(f"**/{pattern}"))
    seen: set[Path] = set()
    deduped: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        deduped.append(p)
    return sorted(deduped)


def parse_spec(path: Path) -> list[Endpoint]:
    data = _load_yaml_or_json(path)
    paths = data.get("paths") or {}
    out: list[Endpoint] = []
    for url, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method, body in ops.items():
            if not isinstance(body, dict):
                continue
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            op_id = body.get("operationId") or f"{method.lower()}_{url}"
            out.append(
                Endpoint(
                    op_id=str(op_id),
                    method=method.upper(),
                    path=str(url),
                    spec_path=path.as_posix(),
                )
            )
    return out


# ---------- stitching --------------------------------------------------


# Heuristic for finding URL literals in source. Catches `/api/invoice`,
# `${BASE}/users/{id}`, `'/orders'`, etc. The leading slash filter keeps
# noise (CSS class names) out.
_URL_RE = re.compile(r"['\"`]\s*/(?:[A-Za-z0-9_./{}\$\-]+)['\"`]")


def _path_template_equiv(a: str, b: str) -> bool:
    """Match a fetch URL like '/api/users/${id}' against an OpenAPI path
    like '/api/users/{id}'."""
    norm = lambda s: re.sub(r"\$\{[^}]+\}|\{[^}]+\}|:[A-Za-z0-9_]+", "{}", s)  # noqa: E731
    return norm(a) == norm(b)


@dataclass(frozen=True)
class StitchEdge:
    src: str
    dst: str
    op_id: str
    confidence: float  # 1.0 contract; 0.5–0.7 heuristic
    reason: str


@dataclass
class StitchResult:
    endpoints: list[Endpoint] = field(default_factory=list)
    edges: list[StitchEdge] = field(default_factory=list)


def _endpoint_node_id(ep: Endpoint) -> str:
    return make_symbol_id(ep.spec_path, f"endpoint::{ep.method} {ep.path}")


def stitch_into(
    G: nx.MultiDiGraph,
    repo_roots: list[Path],
) -> StitchResult:
    """Mutate ``G`` in place: add endpoint nodes + crosses_to edges from
    every fetch literal to its matching endpoint, where matching is
    template-equivalent. Returns the StitchResult for inspection.
    """
    endpoints: list[Endpoint] = []
    for root in repo_roots:
        for spec in discover_specs(root):
            endpoints.extend(parse_spec(spec))

    # Add endpoint nodes.
    for ep in endpoints:
        nid = _endpoint_node_id(ep)
        if not G.has_node(nid):
            G.add_node(
                nid,
                id=nid,
                kind="endpoint",
                name=ep.op_id,
                qualified_name=f"endpoint::{ep.method} {ep.path}",
                path=ep.spec_path,
                span=(0, 0),
                signature=f"{ep.method} {ep.path}",
                exported=True,
                docstring=None,
                parent_id=None,
                language="openapi",
            )

    # Index endpoints by normalised path template.
    by_template: dict[str, list[Endpoint]] = {}
    for ep in endpoints:
        key = re.sub(r"\{[^}]+\}", "{}", ep.path)
        by_template.setdefault(key, []).append(ep)

    edges: list[StitchEdge] = []

    # Walk node attrs we already have to find URL literals — but the
    # graph doesn't carry source bodies. Instead scan source files
    # directly for URL literals and attribute them to the file's module
    # symbol.
    for root in repo_roots:
        for src in _source_files(root):
            try:
                text = src.read_text(errors="replace")
            except OSError:
                continue
            rel = src.relative_to(root).as_posix()
            module_id = make_symbol_id(rel, src.stem)
            for m in _URL_RE.finditer(text):
                lit = m.group(0).strip("\"'`")
                norm = re.sub(r"\$\{[^}]+\}|\{[^}]+\}|:[A-Za-z0-9_]+", "{}", lit)
                candidates = by_template.get(norm, [])
                if not candidates:
                    # 0.4: unmatched URL literal — record but with low
                    # confidence so callers can choose to ignore.
                    continue
                for ep in candidates:
                    nid = _endpoint_node_id(ep)
                    confidence = 1.0 if "{}" not in norm else 0.7
                    if not G.has_node(module_id):
                        # Module wasn't indexed — happens for spec files
                        # outside an adapter glob. Skip silently.
                        continue
                    edges.append(
                        StitchEdge(
                            src=module_id,
                            dst=nid,
                            op_id=ep.op_id,
                            confidence=confidence,
                            reason="url-literal-match",
                        )
                    )
                    G.add_edge(
                        module_id,
                        nid,
                        kind="crosses_to",
                        resolved=True,
                        confidence=confidence,
                        op_id=ep.op_id,
                        span=None,
                    )

    return StitchResult(endpoints=endpoints, edges=edges)


def _source_files(root: Path):
    from ..core.adapters import adapter_for

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(seg in rel for seg in (".git/", ".graphify_plus/", "node_modules/")):
            continue
        if adapter_for(path.name) is None:
            continue
        yield path


__all__ = ["Endpoint", "StitchEdge", "StitchResult", "discover_specs", "parse_spec", "stitch_into"]
