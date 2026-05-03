"""12.4 — confidence drift JSONL log."""

from __future__ import annotations

import networkx as nx

from graphify_plus.audit.drift_log import (
    FAILURE_THRESHOLD,
    FLOOR,
    aggregate,
    apply_to_graph,
    edge_key,
    record_drift,
)


def test_record_and_aggregate_below_threshold(tmp_path):
    (tmp_path / ".graphify_plus").mkdir()
    k = edge_key("a", "b", "imports")
    for _ in range(FAILURE_THRESHOLD - 1):
        record_drift(tmp_path, k, "stale-edge")
    assert aggregate(tmp_path) == {}


def test_aggregate_decays_above_threshold(tmp_path):
    k = edge_key("a", "b", "imports")
    for _ in range(FAILURE_THRESHOLD + 1):
        record_drift(tmp_path, k, "stale-edge")
    out = aggregate(tmp_path)
    assert k in out
    assert out[k]["failures"] == FAILURE_THRESHOLD + 1
    assert out[k]["decay"] > 0


def test_apply_to_graph_floors_at_floor(tmp_path):
    g = nx.MultiDiGraph()
    g.add_node("a")
    g.add_node("b")
    g.add_edge("a", "b", kind="imports", confidence=0.2)
    k = edge_key("a", "b", "imports")
    for _ in range(20):
        record_drift(tmp_path, k, "stale-edge")
    n = apply_to_graph(g, tmp_path)
    assert n == 1
    _u, _v, _k, data = next(iter(g.edges(keys=True, data=True)))
    assert data["confidence"] == FLOOR
