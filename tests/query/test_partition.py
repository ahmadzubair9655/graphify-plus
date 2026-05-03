from __future__ import annotations

import networkx as nx
import pytest

from graphify_plus.query.partition import compute_partition


def _G() -> nx.MultiDiGraph:
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    for n in "abcdefg":
        G.add_node(n, kind="function", name=n, qualified_name=n)
    for u, v in [("a", "b"), ("b", "c"), ("c", "d"), ("c", "e"), ("d", "f"), ("e", "g")]:
        G.add_edge(u, v, kind="calls")
    return G


def test_partition_path_includes_endpoints():
    G = _G()
    p = compute_partition(G, "a", "f")
    assert p.path[0] == "a"
    assert p.path[-1] == "f"
    assert "a" in p.path and "f" in p.path


def test_partition_neighbours_are_one_hop():
    G = _G()
    p = compute_partition(G, "a", "f")
    # neighbours include c's siblings (e) and not deeper-out-of-band nodes.
    assert "e" in p.neighbours
    assert "g" not in p.neighbours  # 2-hop from path


def test_partition_gatekeepers_top_betweenness():
    G = _G()
    p = compute_partition(G, "a", "f")
    assert len(p.gatekeepers) <= 5


def test_partition_unknown_endpoint():
    G = _G()
    with pytest.raises(nx.NodeNotFound):
        compute_partition(G, "a", "zzz")
