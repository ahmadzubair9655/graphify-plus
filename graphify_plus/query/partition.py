"""Topological Partitioning.

Given an ``(entry, target)`` symbol pair on the symbol graph, compute the
*minimum viable context* — shortest path + 1-hop neighbourhood + identified
"Gatekeeper" nodes (high betweenness on the induced subgraph).

Used by Phase 3's token budgeter to pack the smallest, most informative
slice of the codebase into a fixed token budget.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx


@dataclass(frozen=True)
class Partition:
    path: list[str]  # ordered shortest-path symbol ids
    neighbours: list[str]  # 1-hop in/out neighbours (deduped, no path nodes)
    gatekeepers: list[str]  # top-K betweenness nodes on the induced subgraph
    induced: nx.MultiDiGraph

    @property
    def all_ids(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for sid in (*self.path, *self.gatekeepers, *self.neighbours):
            if sid not in seen and sid is not None:
                seen.add(sid)
                out.append(sid)
        return out


def _undirected_view(G: nx.MultiDiGraph) -> nx.Graph:
    """Lossy undirected projection for shortest-path search."""
    UG = nx.Graph()
    UG.add_nodes_from(G.nodes(data=True))
    for u, v in G.edges():
        if not UG.has_edge(u, v):
            UG.add_edge(u, v)
    return UG


def compute_partition(
    G: nx.MultiDiGraph, entry: str, target: str, *, k_gatekeepers: int = 5
) -> Partition:
    """Compute the partition reaching ``target`` from ``entry``.

    Raises ``nx.NetworkXNoPath`` (or ``nx.NodeNotFound``) when the pair
    is unreachable — callers handle that explicitly.
    """
    if entry not in G:
        raise nx.NodeNotFound(f"entry not in graph: {entry!r}")
    if target not in G:
        raise nx.NodeNotFound(f"target not in graph: {target!r}")

    UG = _undirected_view(G)
    path = nx.shortest_path(UG, entry, target)

    neigh: set[str] = set()
    for sid in path:
        for n in G.predecessors(sid):
            neigh.add(n)
        for n in G.successors(sid):
            neigh.add(n)
    for sid in path:
        neigh.discard(sid)

    induced_nodes = list(neigh) + list(path)
    induced = G.subgraph(induced_nodes).copy()

    # Betweenness on the induced subgraph — bounded by induced size, fast.
    if len(induced) >= 3:
        try:
            scores = nx.betweenness_centrality(induced)
            # Stable sort: by score desc, then id asc.
            ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
            gatekeepers = [sid for sid, _ in ranked[:k_gatekeepers]]
        except Exception:
            gatekeepers = []
    else:
        gatekeepers = []

    # Deterministic order for neighbours.
    neighbours = sorted(neigh)
    return Partition(path=path, neighbours=neighbours, gatekeepers=gatekeepers, induced=induced)


__all__ = ["Partition", "compute_partition"]
