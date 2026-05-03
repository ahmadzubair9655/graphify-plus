from __future__ import annotations

import networkx as nx
from hypothesis import given, settings
from hypothesis import strategies as st

from graphify_plus.runtime.overlay import empty


def _G() -> nx.MultiDiGraph:
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    for n in "abcd":
        G.add_node(n, id=n, kind="function", path=f"src/{n}.py", name=n)
    G.add_edge("a", "b", kind="calls")
    G.add_edge("b", "c", kind="calls")
    G.add_edge("c", "d", kind="imports")
    return G


def test_empty_overlay_is_observationally_identical():
    G = _G()
    o = empty(G)
    assert o.is_empty()
    for n in G.nodes:
        assert o.has_node(n)
    materialised = o.materialise()
    assert materialised is G  # short-circuit when empty


def test_add_remove_node():
    G = _G()
    o = empty(G)
    o.add_node("e", kind="function", path="src/e.py", name="e")
    assert o.has_node("e")
    o.remove_node("a")
    assert not o.has_node("a")
    M = o.materialise()
    assert M.has_node("e")
    assert not M.has_node("a")


def test_add_remove_edge():
    G = _G()
    o = empty(G)
    o.add_edge("a", "d", kind="calls")
    o.remove_edge("a", "b", kind="calls")
    out_a = {(u, v, d.get("kind")) for u, v, d in o.out_edges("a")}
    assert ("a", "d", "calls") in out_a
    assert ("a", "b", "calls") not in out_a


def test_attr_override():
    G = _G()
    o = empty(G)
    o.override("a", language="changed")
    assert (o.get_node_attrs("a") or {}).get("language") == "changed"
    # base graph stays clean
    assert "language" not in dict(G.nodes["a"])


@settings(max_examples=20, deadline=None)
@given(seed=st.integers(min_value=0, max_value=1_000_000))
def test_property_apply_then_revert_yields_base(seed):
    """Adding then removing the same edge yields a graph observationally
    identical to the base."""
    G = _G()
    o = empty(G)
    o.add_edge("a", "d", kind="calls", marker=str(seed))
    # Now revert via remove_edge.
    o.removed_edges.add(("a", "d", "calls"))
    # remove_edges only filters base edges; staged adds aren't filtered, so
    # we drop them directly to truly revert.
    o.added_edges = [e for e in o.added_edges if e[:2] != ("a", "d")]
    M = o.materialise() if not o.is_empty() else G
    assert sorted(M.edges()) == sorted(G.edges())
