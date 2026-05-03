"""Build a NetworkX MultiDiGraph from ingest output.

Nodes carry every Symbol attribute; edges carry every Edge attribute.
Unresolved import / call edges land as nodes-by-string-id (so the graph
is closed) — Phase 4 (semantic) and Phase 8 (lockfile) will resolve more
of these.
"""

from __future__ import annotations

import networkx as nx

from .adapters import Edge, Symbol


def build(symbols: list[Symbol], edges: list[Edge]) -> nx.MultiDiGraph:
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    by_id: dict[str, Symbol] = {}
    for s in symbols:
        sid = s["id"]
        by_id[sid] = s
        G.add_node(sid, **s)

    for e in edges:
        src = e.get("src")
        dst = e.get("dst")
        if not src or not dst:
            continue
        if dst not in G:
            # Add a placeholder for unresolved targets so traversal stays closed.
            G.add_node(
                dst,
                id=dst,
                kind="external",
                name=dst,
                qualified_name=dst,
                path="",
                span=(0, 0),
                signature=dst,
                exported=False,
                docstring=None,
                parent_id=None,
                language="external",
            )
        G.add_edge(
            src,
            dst,
            key=f"{e.get('kind')}:{(e.get('span') or (0, 0))[0]}",
            kind=e.get("kind"),
            resolved=bool(e.get("resolved")),
            span=e.get("span"),
        )
    return G


__all__ = ["build"]
