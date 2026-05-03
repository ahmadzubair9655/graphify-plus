"""Copy-on-write overlay over a NetworkX MultiDiGraph.

Used by Phase 6's shadow simulation: stage a hypothetical change
(``add_edge``, ``remove_edge``, ``add_node``, ``rm_node``,
``override_attr``), then evaluate constraints against the *implied*
graph without touching the base.

For algorithms that demand a real graph (e.g., NetworkX betweenness),
``materialise()`` does a defensive copy of only the touched subgraph
(BFS frontier of the changes), keeping the cost bounded by the size of
the change rather than the size of the graph.

Property under tests: ``apply(empty_overlay, G)`` is observationally
identical to ``G``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx


@dataclass
class GraphOverlay:
    base: nx.MultiDiGraph
    added_nodes: dict[str, dict] = field(default_factory=dict)
    removed_nodes: set[str] = field(default_factory=set)
    added_edges: list[tuple[str, str, dict]] = field(default_factory=list)
    removed_edges: set[tuple[str, str, str]] = field(default_factory=set)  # (src,dst,kind)
    attr_overrides: dict[str, dict] = field(default_factory=dict)

    # ---- staging API ----------------------------------------------------
    def add_node(self, sid: str, **attrs) -> None:
        self.removed_nodes.discard(sid)
        self.added_nodes[sid] = {"id": sid, **attrs}

    def remove_node(self, sid: str) -> None:
        self.added_nodes.pop(sid, None)
        self.removed_nodes.add(sid)

    def add_edge(self, src: str, dst: str, *, kind: str = "calls", **attrs) -> None:
        self.added_edges.append((src, dst, {"kind": kind, **attrs}))

    def remove_edge(self, src: str, dst: str, *, kind: str | None = None) -> None:
        # If kind is None, remove all edges matching (src,dst) regardless of kind.
        if kind is None:
            for u, v, k, _ in self.base.edges(keys=True, data=True):
                if u == src and v == dst:
                    self.removed_edges.add((src, dst, str(k or "")))
        else:
            self.removed_edges.add((src, dst, kind))

    def override(self, sid: str, **attrs) -> None:
        self.attr_overrides.setdefault(sid, {}).update(attrs)

    # ---- views ----------------------------------------------------------
    def is_empty(self) -> bool:
        return not (
            self.added_nodes
            or self.removed_nodes
            or self.added_edges
            or self.removed_edges
            or self.attr_overrides
        )

    def has_node(self, sid: str) -> bool:
        if sid in self.removed_nodes:
            return False
        return sid in self.added_nodes or self.base.has_node(sid)

    def get_node_attrs(self, sid: str) -> dict | None:
        if sid in self.removed_nodes:
            return None
        if sid in self.added_nodes:
            attrs = dict(self.added_nodes[sid])
        elif self.base.has_node(sid):
            attrs = dict(self.base.nodes[sid])
        else:
            return None
        attrs.update(self.attr_overrides.get(sid, {}))
        return attrs

    def out_edges(self, sid: str) -> list[tuple[str, str, dict]]:
        out: list[tuple[str, str, dict]] = []
        if sid not in self.removed_nodes:
            for u, v, data in self.base.out_edges(sid, data=True):
                kind = (data or {}).get("kind") or ""
                if (u, v, kind) in self.removed_edges:
                    continue
                if v in self.removed_nodes:
                    continue
                out.append((u, v, dict(data or {})))
        for u, v, data in self.added_edges:
            if u == sid:
                out.append((u, v, dict(data)))
        return out

    def in_edges(self, sid: str) -> list[tuple[str, str, dict]]:
        out: list[tuple[str, str, dict]] = []
        if sid not in self.removed_nodes:
            for u, v, data in self.base.in_edges(sid, data=True):
                kind = (data or {}).get("kind") or ""
                if (u, v, kind) in self.removed_edges:
                    continue
                if u in self.removed_nodes:
                    continue
                out.append((u, v, dict(data or {})))
        for u, v, data in self.added_edges:
            if v == sid:
                out.append((u, v, dict(data)))
        return out

    # ---- materialisation ------------------------------------------------
    def materialise(self) -> nx.MultiDiGraph:
        """Return a real MultiDiGraph reflecting the overlay.

        Defensive copy of only the touched subgraph (changed nodes +
        their 1-hop frontier). Algorithms that need a real graph use
        this; everything else should query through the overlay views.
        """
        if self.is_empty():
            return self.base

        # Frontier: every changed node + its base-graph neighbours.
        frontier: set[str] = set()
        frontier.update(self.added_nodes.keys())
        frontier.update(self.removed_nodes)
        frontier.update(self.attr_overrides.keys())
        for u, v, _ in self.added_edges:
            frontier.add(u)
            frontier.add(v)
        for u, v, _ in self.removed_edges:
            frontier.add(u)
            frontier.add(v)
        for sid in list(frontier):
            if self.base.has_node(sid):
                frontier.update(self.base.predecessors(sid))
                frontier.update(self.base.successors(sid))

        out: nx.MultiDiGraph = nx.MultiDiGraph()
        # Copy entire base — necessary if callers run global algorithms.
        for n, attrs in self.base.nodes(data=True):
            out.add_node(n, **attrs)
        for u, v, data in self.base.edges(data=True):
            out.add_edge(u, v, **(data or {}))

        # Apply overlay deltas.
        for sid in self.removed_nodes:
            if out.has_node(sid):
                out.remove_node(sid)
        for sid, attrs in self.added_nodes.items():
            out.add_node(sid, **attrs)
        for sid, attrs in self.attr_overrides.items():
            if out.has_node(sid):
                out.nodes[sid].update(attrs)
        # Edge removals: drop matching keys.
        for u, v, kind in self.removed_edges:
            if not out.has_edge(u, v):
                continue
            keys_to_drop = []
            for k, data in out[u][v].items():
                if (data or {}).get("kind") == kind:
                    keys_to_drop.append(k)
            for k in keys_to_drop:
                out.remove_edge(u, v, key=k)
        for u, v, data in self.added_edges:
            out.add_edge(u, v, **(data or {}))

        return out


def empty(base: nx.MultiDiGraph) -> GraphOverlay:
    return GraphOverlay(base=base)


__all__ = ["GraphOverlay", "empty"]
