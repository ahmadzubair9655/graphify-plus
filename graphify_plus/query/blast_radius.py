"""Blast radius — given a vulnerable package name, BFS outward along
``depends_on`` and ``imports`` edges and return every reachable in-repo
symbol ranked by import depth.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx


@dataclass(frozen=True)
class BlastHit:
    symbol_id: str
    qualified_name: str
    path: str
    depth: int


@dataclass(frozen=True)
class BlastReport:
    package: str
    hits: list[BlastHit]
    suggestion: str


def _find_dep_node(G: nx.MultiDiGraph, package: str) -> str | None:
    target = package.lower()
    for sid, attrs in G.nodes(data=True):
        a = attrs or {}
        if a.get("kind") != "dependency":
            continue
        if (a.get("name") or "").lower() == target:
            return sid
    return None


def trace(G: nx.MultiDiGraph, package: str) -> BlastReport:
    start = _find_dep_node(G, package)
    if start is None:
        return BlastReport(
            package=package,
            hits=[],
            suggestion=(
                f"No dependency node for {package!r}. "
                "Run 'gp enrich --lockfile' first or check the package name."
            ),
        )

    # BFS outward along import / call / depends_on edges, but reverse
    # direction: find every symbol that depends on this package
    # transitively.
    seen: dict[str, int] = {start: 0}
    queue: list[str] = [start]
    hits: list[BlastHit] = []
    while queue:
        sid = queue.pop(0)
        depth = seen[sid]
        for u, _, data in G.in_edges(sid, data=True):
            kind = (data or {}).get("kind") or ""
            if kind not in {"depends_on", "imports", "calls"}:
                continue
            if u in seen:
                continue
            seen[u] = depth + 1
            queue.append(u)
            attrs = G.nodes[u]
            if attrs.get("kind") not in {"dependency", "external", "module"}:
                hits.append(
                    BlastHit(
                        symbol_id=u,
                        qualified_name=attrs.get("qualified_name") or u,
                        path=attrs.get("path") or "",
                        depth=depth + 1,
                    )
                )
    hits.sort(key=lambda h: (h.depth, h.qualified_name))
    suggestion = (
        f"Upgrade {package} to a patched version, or isolate the import "
        f"behind a single boundary module so the rest of the graph "
        f"depends only on that boundary."
    )
    return BlastReport(package=package, hits=hits, suggestion=suggestion)


__all__ = ["BlastHit", "BlastReport", "trace"]
