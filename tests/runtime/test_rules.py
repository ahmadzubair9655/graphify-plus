from __future__ import annotations

import networkx as nx

from graphify_plus.runtime.overlay import empty
from graphify_plus.runtime.rules import RuleSet, assign_layer, evaluate, grade


def _G() -> nx.MultiDiGraph:
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    G.add_node("ui1", path="frontend/x.tsx", kind="function")
    G.add_node("api1", path="backend/api/handler.py", kind="function")
    G.add_node("db1", path="backend/db/client.py", kind="function")
    G.add_edge("ui1", "api1", kind="calls")
    return G


def _rules_no_ui_to_db():
    return RuleSet(
        layers={
            "ui": ["frontend/**"],
            "api": ["backend/api/**"],
            "database": ["backend/db/**"],
        },
        rules=[
            {
                "id": "no-ui-to-db",
                "severity": "error",
                "forbid_edge": {
                    "from_layer": "ui",
                    "to_layer": "database",
                    "kinds": ["imports", "calls"],
                },
            }
        ],
    )


def test_layer_assignment():
    layers = {"ui": ["frontend/**"], "api": ["backend/api/**"]}
    assert assign_layer("frontend/x.tsx", layers) == "ui"
    assert assign_layer("backend/api/foo.py", layers) == "api"
    assert assign_layer("scripts/build.sh", layers) is None


def test_clean_graph_yields_no_violations():
    G = _G()
    rs = _rules_no_ui_to_db()
    vios = evaluate(empty(G), rs)
    assert vios == []
    assert grade(vios) == "A"


def test_overlay_introduces_ui_to_db_edge_is_caught():
    G = _G()
    rs = _rules_no_ui_to_db()
    o = empty(G)
    o.add_edge("ui1", "db1", kind="imports")
    vios = evaluate(o, rs)
    assert any(v.rule_id == "no-ui-to-db" and v.src == "ui1" for v in vios)
    assert grade(vios) in {"C", "D", "F"}


def test_forbid_cycle():
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    G.add_node("a", path="x.py", kind="module")
    G.add_node("b", path="y.py", kind="module")
    G.add_edge("a", "b", kind="imports")
    G.add_edge("b", "a", kind="imports")
    rs = RuleSet(
        rules=[
            {
                "id": "no-circular-imports",
                "severity": "error",
                "forbid_cycle": {"kinds": ["imports"]},
            }
        ]
    )
    vios = evaluate(empty(G), rs)
    assert any(v.rule_id == "no-circular-imports" for v in vios)
