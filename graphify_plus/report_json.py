"""
graphify_plus.report_json — Minimal serialization shim used by tests + the
forthcoming `enhance` pipeline.

For now this only exposes `save_enhanced_graph(G, out_dir)` which writes a
networkx graph to ``{out_dir}/graph_enhanced.json`` in the same JSON shape
graphify itself emits (``nodes`` + ``edges`` lists). The full enhanced report
(`report.json` with health score) is part of the v2 enhance pipeline that
isn't yet shipped — this shim covers the part the v3 modules need.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import networkx as nx


def graph_to_dict(G: nx.Graph) -> dict[str, Any]:
    """Serialize a networkx graph to graphify's `{nodes, edges}` JSON shape."""
    nodes: list[dict[str, Any]] = []
    for node_id, attrs in G.nodes(data=True):
        nodes.append({"id": node_id, **attrs})
    edges: list[dict[str, Any]] = []
    for src, dst, attrs in G.edges(data=True):
        edges.append({"source": src, "target": dst, **attrs})
    return {"nodes": nodes, "edges": edges}


def save_enhanced_graph(G: nx.Graph, out_dir: str | Path, filename: str = "graph_enhanced.json") -> Path:
    """Write the graph to ``{out_dir}/{filename}`` and return the path written."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / filename
    target.write_text(json.dumps(graph_to_dict(G), indent=2, default=str))
    return target
