from __future__ import annotations

import networkx as nx

from graphify_plus.query.prune import (
    find_dead_code,
    find_deprecated,
    find_orphans,
    report,
)


def _node(G, sid, **attrs):
    G.add_node(sid, id=sid, **attrs)


def test_dead_code_skips_protected():
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    _node(G, "mod", kind="module", exported=True)
    _node(G, "exp", kind="function", exported=True)
    _node(G, "live", kind="function", exported=False)
    _node(G, "dead", kind="function", exported=False)
    G.add_edge("live", "exp", kind="calls")  # 'live' is alive, 'dead' is not
    dead = find_dead_code(G)
    assert "dead" in dead
    assert "exp" not in dead
    assert "mod" not in dead
    assert "live" not in dead


def test_orphans_singletons_only_unprotected():
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    _node(G, "lonely", kind="function", exported=False)
    _node(G, "module_alone", kind="module", exported=True)
    _node(G, "a", kind="function", exported=False)
    _node(G, "b", kind="function", exported=False)
    G.add_edge("a", "b", kind="calls")
    orphans = find_orphans(G)
    assert "lonely" in orphans
    assert "module_alone" not in orphans
    assert "a" not in orphans


def test_deprecated_via_docstring_or_path():
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    _node(G, "doc_dep", kind="function", docstring="Deprecated. Use bar.")
    _node(G, "path_dep", kind="function", path="src/legacy/foo.py", docstring="")
    _node(G, "ok", kind="function", docstring="")
    deps = find_deprecated(G)
    assert "doc_dep" in deps
    assert "path_dep" in deps
    assert "ok" not in deps


def test_report_aggregates():
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    # Two connected nodes — neither is dead nor orphan, but one is
    # deprecated. Aggressive=True is the only way to exclude it.
    _node(G, "caller", kind="function", exported=True)
    _node(G, "dep", kind="function", exported=True, docstring="DEPRECATED — old API.")
    G.add_edge("caller", "dep", kind="calls")
    rep = report(G)
    assert "dep" in rep.deprecated
    assert "dep" not in rep.dead
    assert "dep" not in rep.orphans
    assert "dep" not in rep.all_ids(aggressive=False)
    assert "dep" in rep.all_ids(aggressive=True)
