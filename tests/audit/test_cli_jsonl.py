"""`gp audit` reads both legacy graph.json and v5+ graph_symbols.jsonl."""

from __future__ import annotations

import json
from pathlib import Path

from graphify_plus.audit.cli import _load_graph


def test_load_graph_reads_jsonl(tmp_path: Path):
    p = tmp_path / "graph_symbols.jsonl"
    # Mirror what `gp init` actually writes: dict spread overrides the
    # leading "kind" marker, so symbols carry their own kind (module,
    # function, ...) and edges carry their relation kind (calls, etc.).
    lines = [
        {"_meta": {"kind": "header", "version": 1, "symbols": 2, "edges": 1}},
        {"id": "a", "kind": "function", "name": "A", "language": "python"},
        {"id": "b", "kind": "function", "name": "B", "language": "python"},
        {"src": "a", "dst": "b", "kind": "calls", "confidence": 0.9},
    ]
    p.write_text("\n".join(json.dumps(rec) for rec in lines) + "\n")

    G = _load_graph(p)
    assert set(G.nodes) == {"a", "b"}
    assert G.has_edge("a", "b")
    assert G.nodes["a"]["name"] == "A"
    assert G.nodes["a"]["language"] == "python"


def test_load_graph_still_reads_legacy_json(tmp_path: Path):
    p = tmp_path / "graph.json"
    p.write_text(
        json.dumps(
            {
                "nodes": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
                "edges": [{"source": "a", "target": "b"}],
            }
        )
    )
    G = _load_graph(p)
    assert set(G.nodes) == {"a", "b"}
    assert G.has_edge("a", "b")
