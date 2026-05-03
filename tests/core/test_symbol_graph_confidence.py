"""Confidence propagation through ``symbol_graph.build``."""

from __future__ import annotations

from graphify_plus.core.adapters.base import CONF_EXACT, CONF_FALLBACK, Edge, Symbol
from graphify_plus.core.symbol_graph import build


def test_build_propagates_confidence_to_edge_attrs():
    sym_a: Symbol = {
        "id": "a",
        "kind": "module",
        "name": "a",
        "qualified_name": "a",
        "path": "a.py",
        "span": (0, 0),
        "signature": "",
        "exported": True,
        "docstring": None,
        "parent_id": None,
        "language": "python",
    }
    sym_b: Symbol = {**sym_a, "id": "b", "qualified_name": "b", "name": "b"}
    edges: list[Edge] = [
        Edge(src="a", dst="b", kind="contains", resolved=True, span=None, confidence=CONF_EXACT),
        Edge(src="a", dst="ext", kind="imports", resolved=False, span=None, confidence=CONF_FALLBACK),
    ]
    g = build([sym_a, sym_b], edges)
    confidences = sorted(d["confidence"] for _u, _v, _k, d in g.edges(data=True, keys=True))
    assert confidences == [CONF_FALLBACK, CONF_EXACT]


def test_build_defaults_missing_confidence_to_fallback():
    sym: Symbol = {
        "id": "a",
        "kind": "module",
        "name": "a",
        "qualified_name": "a",
        "path": "a.py",
        "span": (0, 0),
        "signature": "",
        "exported": True,
        "docstring": None,
        "parent_id": None,
        "language": "python",
    }
    legacy: Edge = {"src": "a", "dst": "x", "kind": "imports", "resolved": False, "span": None}
    g = build([sym], [legacy])
    _u, _v, _k, d = next(iter(g.edges(data=True, keys=True)))
    assert d["confidence"] == CONF_FALLBACK
