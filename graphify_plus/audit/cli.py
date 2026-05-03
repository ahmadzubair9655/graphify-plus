"""
graphify_plus.audit.cli — CLI wrapper around the adversarial audit probes.

Usage:
    graphify-plus audit path/to/graph.json
    graphify-plus audit path/to/graph.json --json-output audit.json
    graphify-plus audit path/to/graph.json --only confidence_drift
    graphify-plus audit path/to/graph.json --fail-below B --quiet
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import networkx as nx

from graphify_plus.audit.probe import (
    audit_meets_threshold,
    format_audit_report,
    probe_centrality_drift,
    probe_confidence_drift,
    probe_edge_deletion_stability,
    probe_lonely_inferred_edges,
    probe_modularity_quality,
    probe_rename_sensitivity,
    probe_structural_fragility,
    run_audit,
)

# Map --only argument values to their probe functions, so callers can
# request a subset instead of the full grade pipeline.
_PROBE_FUNCS = {
    "edge_deletion_stability": probe_edge_deletion_stability,
    "confidence_drift": probe_confidence_drift,
    "rename_sensitivity": probe_rename_sensitivity,
    "structural_fragility": probe_structural_fragility,
    "lonely_inferred_edges": probe_lonely_inferred_edges,
    "modularity_quality": probe_modularity_quality,
    "centrality_drift": probe_centrality_drift,
}


def _load_graph(path: Path) -> nx.Graph:
    with open(path) as f:
        data = json.load(f)
    G = nx.Graph()
    for n in data.get("nodes", []):
        node_id = n.get("id")
        attrs = {k: v for k, v in n.items() if k != "id"}
        G.add_node(node_id, **attrs)
    for e in data.get("edges", data.get("links", [])):
        attrs = {k: v for k, v in e.items() if k not in ("source", "target")}
        G.add_edge(e.get("source"), e.get("target"), **attrs)
    return G


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="graphify-plus audit",
        description="Adversarial audit of a graphify graph.json — what should you not trust?",
    )
    parser.add_argument("graph", help="Path to graph.json (or graph_enhanced.json)")
    parser.add_argument(
        "--json-output",
        metavar="PATH",
        help="Write the full machine-readable audit JSON to PATH",
    )
    parser.add_argument(
        "--fail-below",
        choices=["A", "B", "C", "D", "F"],
        default=None,
        help="Exit non-zero if the overall grade is below this letter",
    )
    parser.add_argument(
        "--only",
        choices=list(_PROBE_FUNCS.keys()),
        action="append",
        default=None,
        help="Run only the named probe(s). Repeat for multiple. "
             "When set, output JSON contains only those probes.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress text report on stdout")

    args = parser.parse_args(argv)

    graph_path = Path(args.graph)
    if not graph_path.exists():
        if not args.quiet:
            print(f"error: graph file not found: {graph_path}", file=sys.stderr)
        return 2

    G = _load_graph(graph_path)

    # If the caller asked for a subset, run only those probes and emit a
    # narrower JSON. The tests assert that excluded probes are NOT present.
    if args.only:
        partial: dict[str, Any] = {}
        for probe_name in args.only:
            partial[probe_name] = _PROBE_FUNCS[probe_name](G)
        if args.json_output:
            Path(args.json_output).write_text(json.dumps(partial, indent=2, default=str))
        if not args.quiet:
            print(json.dumps(partial, indent=2, default=str))
        return 0

    # Full audit pipeline.
    result = run_audit(G)

    if args.json_output:
        Path(args.json_output).write_text(json.dumps(result, indent=2, default=str))

    if not args.quiet:
        print(format_audit_report(result))

    if args.fail_below and not audit_meets_threshold(result, args.fail_below):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
