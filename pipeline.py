"""
pipeline.py — Enhanced pipeline orchestrator

Fix H: structured warnings list collected from every module, surfaced in report.json
Fix I: save_enhanced_graph() called at end — enriched graph persists to disk
Fix B: auto_tag_namespaces called before reconcile (via reconcile.py change)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

import networkx as nx

try:
    from graphify.build import build_graph
    from graphify.cluster import cluster
    from graphify.analyze import analyze
    from graphify.report import render_report
    from graphify.export import export
    _GRAPHIFY_AVAILABLE = True
except ImportError:
    _GRAPHIFY_AVAILABLE = False

from graphify_enhanced.temporal import enrich_graph_with_temporal
from graphify_enhanced.contradict import detect_contradictions, format_contradiction_report
from graphify_enhanced.causal import top_causal_chains
from graphify_enhanced.report_json import (
    build_report_json, save_report_json, save_enhanced_graph, print_stats
)


def _warn(warnings: list, module: str, op: str, reason: str) -> None:
    """Append a structured warning (Fix H)."""
    warnings.append({"module": module, "op": op, "reason": reason})


def run_enhanced_pipeline(
    G: nx.Graph,
    out_dir: Path,
    corpus_root: Optional[Path] = None,
    llm_fn: Optional[Callable] = None,
    run_causal: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Apply all enhancements to an already-built NetworkX graph G.

    Fix H: all module warnings collected and threaded into report.json.
    Fix I: enriched graph saved to graphify-out/graph_enhanced.json.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[dict] = []   # Fix H

    if verbose:
        print("🔧 graphify-enhanced: running enhancement pipeline...")

    # --- Enhancement 1: Temporal enrichment ---
    if verbose:
        print("  [1/5] Temporal edge enrichment (bulk git log)...")
    try:
        temporal_summary = enrich_graph_with_temporal(
            G, corpus_root=corpus_root or out_dir.parent,
        )
    except Exception as exc:
        _warn(warnings, "temporal", "enrich_graph_with_temporal", str(exc))
        temporal_summary = {"nodes_enriched": 0, "supersedes_edges_added": 0,
                            "repo_root": None, "skipped_no_git": True}

    if verbose:
        if temporal_summary["skipped_no_git"]:
            print("       ⚠️  Not a git repo — temporal data skipped.")
        else:
            print(f"       ✓ {temporal_summary['nodes_enriched']} nodes enriched, "
                  f"{temporal_summary['supersedes_edges_added']} supersedes edges added.")

    # --- Enhancement 4: Contradiction detection ---
    if verbose:
        print("  [2/5] Contradiction detection (dynamic pair inference)...")
    try:
        contradiction_report = detect_contradictions(G, annotate_graph=True)
    except Exception as exc:
        _warn(warnings, "contradict", "detect_contradictions", str(exc))
        contradiction_report = {"contradictions": [], "total": 0,
                                "by_type": {}, "edges_added": 0, "exclusive_pairs_used": 0}

    if verbose:
        total = contradiction_report["total"]
        pairs_used = contradiction_report.get("exclusive_pairs_used", 0)
        if total:
            print(f"       ⚠️  {total} contradictions found "
                  f"({contradiction_report['by_type']}) — {pairs_used} pairs checked.")
        else:
            print(f"       ✓ No contradictions ({pairs_used} pairs checked).")

    # --- Enhancement 3: Causal chains (opt-in) ---
    causal_chains = []
    if run_causal and llm_fn is not None:
        if verbose:
            print("  [3/5] Causal chain extraction (smart node selection)...")
        try:
            from graphify_enhanced.causal import extract_causal_chains
            causal_result = extract_causal_chains(G, llm_fn=llm_fn)
            causal_chains = top_causal_chains(G)
            if verbose:
                print(f"       ✓ {causal_result['edges_added']} causal edges, "
                      f"{len(causal_chains)} root chains.")
        except Exception as exc:
            _warn(warnings, "causal", "extract_causal_chains", str(exc))
    else:
        if verbose:
            print("  [3/5] Causal chains — skipped (pass run_causal=True to enable).")
        causal_chains = top_causal_chains(G)

    # --- Enhancement 7: Structured report.json ---
    if verbose:
        print("  [4/5] Building structured report.json + health score...")

    analysis = None
    analysis_path = out_dir / "analysis.json"
    if analysis_path.exists():
        try:
            with analysis_path.open() as f:
                analysis = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            _warn(warnings, "report_json", "load_analysis", str(exc))

    report = build_report_json(
        G, analysis=analysis,
        contradiction_report=contradiction_report,
        causal_chains=causal_chains,
        temporal_summary=temporal_summary,
        warnings=warnings,   # Fix H
    )
    report_path = save_report_json(report, out_dir)

    # Append to GRAPH_REPORT.md
    graph_report_path = out_dir / "GRAPH_REPORT.md"
    if graph_report_path.exists():
        try:
            contradiction_md = format_contradiction_report(
                contradiction_report.get("contradictions", [])
            )
            with graph_report_path.open("a", encoding="utf-8") as f:
                f.write("\n\n" + contradiction_md)
            if causal_chains:
                causal_md = _format_causal_chains_md(causal_chains)
                with graph_report_path.open("a", encoding="utf-8") as f:
                    f.write("\n\n" + causal_md)
        except OSError as exc:
            _warn(warnings, "pipeline", "append_graph_report_md", str(exc))

    # Fix I: persist enriched graph to disk
    if verbose:
        print("  [5/5] Saving enriched graph to graph_enhanced.json...")
    try:
        enhanced_graph_path = save_enhanced_graph(G, out_dir)
        if verbose:
            print(f"       ✓ {enhanced_graph_path}")
    except Exception as exc:
        _warn(warnings, "pipeline", "save_enhanced_graph", str(exc))
        enhanced_graph_path = None

    if verbose:
        print(f"       ✓ report.json → {report_path}")
        if warnings:
            print(f"       🔔 {len(warnings)} warning(s) — see report.json['warnings']")
        print_stats(report)

    return {
        "temporal": temporal_summary,
        "contradictions": contradiction_report,
        "causal_chains": causal_chains,
        "report": report,
        "report_path": report_path,
        "enhanced_graph_path": str(enhanced_graph_path) if enhanced_graph_path else None,
        "warnings": warnings,
    }


def _format_causal_chains_md(chains: list[dict]) -> str:
    if not chains:
        return ""
    lines = ["## 🔗 Causal Chains\n",
             "Decisions with the most downstream dependents:\n"]
    for c in chains:
        path_str = " → ".join(c.get("path_labels", []))
        lines.append(
            f"- **{c['root_label']}** ({c['downstream_count']} downstream): `{path_str}`"
        )
    return "\n".join(lines)


def enhance_existing_graph(
    graph_json_path: Path,
    corpus_root: Optional[Path] = None,
    verbose: bool = True,
) -> dict:
    """
    Load an existing graph.json from graphify and run all enhancements.
    Writes report.json and graph_enhanced.json alongside graph.json.
    """
    graph_json_path = Path(graph_json_path)
    out_dir = graph_json_path.parent

    if not graph_json_path.exists():
        raise FileNotFoundError(f"graph.json not found: {graph_json_path}")

    with graph_json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # Fix A: restore directed flag if present in the original file
    directed = data.get("_directed", False)
    G: nx.Graph = nx.DiGraph() if directed else nx.Graph()

    for node in data.get("nodes", []):
        nid = node.get("id") or node.get("label", "")
        G.add_node(nid, **{k: v for k, v in node.items() if k != "id"})

    for edge in data.get("edges", []):
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        if src and tgt:
            G.add_edge(src, tgt, **{k: v for k, v in edge.items()
                                     if k not in ("source", "target")})

    if verbose:
        print(f"📂 Loaded {'directed' if directed else 'undirected'} graph: "
              f"{G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    return run_enhanced_pipeline(
        G, out_dir=out_dir, corpus_root=corpus_root, verbose=verbose,
    )
