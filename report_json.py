"""
report_json.py — Enhancement #7: Structured report.json companion

Fix G: health score (0–100) computed from graph quality signals.
Fix H: warnings list threaded through from all modules, surfaced in report.
"""

from __future__ import annotations

import json, time
from pathlib import Path
from typing import Any, Optional
import networkx as nx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _top_god_nodes(G, top_n=10):
    ranked = sorted(G.degree(), key=lambda x: x[1], reverse=True)[:top_n]
    return [{
        "id": nid, "label": G.nodes[nid].get("label", nid),
        "degree": deg, "source_file": G.nodes[nid].get("source_file",""),
        "community": G.nodes[nid].get("community"),
        "first_seen": G.nodes[nid].get("first_seen"),
        "contradiction": G.nodes[nid].get("contradiction", False),
        "flagged_ambiguous": G.nodes[nid].get("flagged_ambiguous", False),
    } for nid, deg in ranked]


def _community_summary(G):
    communities: dict[Any, list] = {}
    for nid, data in G.nodes(data=True):
        comm = data.get("community")
        if comm is not None:
            communities.setdefault(comm, []).append((nid, G.degree(nid)))
    result = []
    for comm_id, nodes in sorted(communities.items(), key=lambda x: len(x[1]), reverse=True):
        top = sorted(nodes, key=lambda x: x[1], reverse=True)[:5]
        result.append({
            "community_id": comm_id, "size": len(nodes),
            "top_nodes": [{"id": nid, "label": G.nodes[nid].get("label", nid), "degree": deg}
                          for nid, deg in top],
        })
    return result


def _edge_type_breakdown(G):
    counts: dict[str, int] = {}
    for _, _, d in G.edges(data=True):
        rel = d.get("relation","unknown")
        counts[rel] = counts.get(rel, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))


def _confidence_breakdown(G):
    counts = {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0, "other": 0}
    for _, _, d in G.edges(data=True):
        conf = d.get("confidence","other")
        counts[conf if conf in counts else "other"] += 1
    return counts


def _surprising_connections(G, analysis=None, top_n=10):
    if analysis and "surprises" in analysis:
        return [{
            "source_label": s.get("source_label") or s.get("source"),
            "target_label": s.get("target_label") or s.get("target"),
            "relation": s.get("relation",""),
            "why": s.get("why") or s.get("reason",""),
            "confidence_score": s.get("confidence_score"),
        } for s in analysis["surprises"][:top_n]]
    result = []
    for u, v, data in G.edges(data=True):
        if data.get("confidence") != "INFERRED":
            continue
        uc = G.nodes[u].get("community")
        vc = G.nodes[v].get("community")
        if uc is not None and vc is not None and uc != vc:
            score = data.get("confidence_score", 0.5)
            if isinstance(score, float) and score >= 0.70:
                result.append({
                    "source_label": G.nodes[u].get("label", u),
                    "target_label": G.nodes[v].get("label", v),
                    "relation": data.get("relation",""),
                    "why": "Cross-community INFERRED edge with high confidence.",
                    "confidence_score": score,
                })
    result.sort(key=lambda x: x.get("confidence_score") or 0, reverse=True)
    return result[:top_n]


def _most_active_nodes(G, top_n=5):
    nodes = [(nid, d) for nid, d in G.nodes(data=True) if d.get("commit_count",0) > 0]
    nodes.sort(key=lambda x: x[1].get("commit_count",0), reverse=True)
    return [{"id": nid, "label": d.get("label",nid), "commit_count": d.get("commit_count"),
             "last_modified": d.get("last_modified"), "last_author": d.get("last_author")}
            for nid, d in nodes[:top_n]]


# ---------------------------------------------------------------------------
# Fix G: Health score
# ---------------------------------------------------------------------------

def compute_health_score(
    G: nx.Graph,
    contradiction_report: Optional[dict] = None,
    temporal_summary: Optional[dict] = None,
    causal_chains: Optional[list] = None,
) -> dict:
    """
    Compute a 0–100 health score from graph quality signals.

    Components (weights sum to 100):
      - Trust ratio (EXTRACTED / total edges):    30 pts
      - Contradiction penalty:                    25 pts
      - Temporal coverage:                        20 pts
      - Causal coverage:                          15 pts
      - Ambiguity penalty (flagged nodes):        10 pts
    """
    n_edges = G.number_of_edges()
    n_nodes = G.number_of_nodes()
    if n_nodes == 0:
        return {"score": 0, "grade": "F", "components": {}, "summary": "Empty graph."}

    conf = _confidence_breakdown(G)
    extracted = conf.get("EXTRACTED", 0)
    total_conf = extracted + conf.get("INFERRED", 0) + conf.get("AMBIGUOUS", 0)
    trust_ratio = extracted / total_conf if total_conf else 0.5
    trust_pts = round(trust_ratio * 30)

    # Contradiction penalty
    n_contradictions = contradiction_report.get("total", 0) if contradiction_report else 0
    # 0 contradictions → full 25 pts; scales down (floor 0)
    contradict_pts = max(0, 25 - min(n_contradictions * 3, 25))

    # Temporal coverage
    nodes_enriched = temporal_summary.get("nodes_enriched", 0) if temporal_summary else 0
    skipped_git    = temporal_summary.get("skipped_no_git", True) if temporal_summary else True
    if skipped_git:
        temporal_pts = 10  # neutral — not penalised for no git
    else:
        temporal_pts = round((nodes_enriched / max(n_nodes, 1)) * 20)

    # Causal coverage — high-degree nodes that have causal edges
    god_node_ids = {nid for nid, deg in sorted(G.degree(), key=lambda x: x[1], reverse=True)[:10]}
    causal_node_ids = {
        v for _, v, d in G.edges(data=True)
        if d.get("causal_edge") or d.get("relation") in (
            "caused_by","mandated_by","required_by","response_to","evolved_from"
        )
    }
    causal_coverage = len(god_node_ids & causal_node_ids) / max(len(god_node_ids), 1)
    causal_pts = round(causal_coverage * 15)

    # Ambiguity penalty
    flagged = sum(1 for _, d in G.nodes(data=True) if d.get("flagged_ambiguous"))
    ambiguity_pts = max(0, 10 - min(flagged * 2, 10))

    total = trust_pts + contradict_pts + temporal_pts + causal_pts + ambiguity_pts

    if total >= 85: grade = "A"
    elif total >= 70: grade = "B"
    elif total >= 55: grade = "C"
    elif total >= 40: grade = "D"
    else: grade = "F"

    drags = []
    if trust_pts < 15:      drags.append(f"low trust ratio ({round(trust_ratio*100)}% EXTRACTED)")
    if contradict_pts < 15: drags.append(f"{n_contradictions} contradictions")
    if causal_pts < 8:      drags.append("few causal chains on god nodes")
    if ambiguity_pts < 5:   drags.append(f"{flagged} flagged-ambiguous nodes")

    summary = f"Health {total}/100 ({grade})"
    if drags:
        summary += f" — main drag{'s' if len(drags)>1 else ''}: {', '.join(drags)}"

    return {
        "score": total, "grade": grade,
        "components": {
            "trust_ratio_pts": trust_pts,
            "contradiction_pts": contradict_pts,
            "temporal_coverage_pts": temporal_pts,
            "causal_coverage_pts": causal_pts,
            "ambiguity_pts": ambiguity_pts,
        },
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Fix I: export enhanced graph back to graph.json schema
# ---------------------------------------------------------------------------

def save_enhanced_graph(G: nx.Graph, out_dir: Path) -> Path:
    """
    Serialise the enriched NetworkX graph back to graphify's graph.json schema.
    Writes graphify-out/graph_enhanced.json (separate from original graph.json).

    All new node attributes (first_seen, contradiction, annotations, causal_edge, etc.)
    and new edge types (supersedes, CONTRADICTS, cross_repo_equivalent_to, caused_by)
    are preserved.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    nodes = []
    for nid, data in G.nodes(data=True):
        node = {"id": nid}
        node.update({k: v for k, v in data.items()
                     if isinstance(v, (str, int, float, bool, list, dict, type(None)))})
        nodes.append(node)

    edges = []
    if G.is_multigraph():
        for u, v, key, data in G.edges(keys=True, data=True):
            edge = {"source": u, "target": v}
            edge.update({k: v for k, v in data.items()
                         if isinstance(v, (str, int, float, bool, list, dict, type(None)))})
            edges.append(edge)
    else:
        for u, v, data in G.edges(data=True):
            edge = {"source": u, "target": v}
            edge.update({k: v for k, v in data.items()
                         if isinstance(v, (str, int, float, bool, list, dict, type(None)))})
            edges.append(edge)

    graph_data = {
        "_schema": "graphify-enhanced-1.0",
        "_generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "_directed": G.is_directed(),
        "nodes": nodes,
        "edges": edges,
    }

    out_path = out_dir / "graph_enhanced.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(graph_data, f, indent=2, ensure_ascii=False)
    return out_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_report_json(
    G: nx.Graph,
    analysis: Optional[dict] = None,
    contradiction_report: Optional[dict] = None,
    causal_chains: Optional[list] = None,
    temporal_summary: Optional[dict] = None,
    warnings: Optional[list[dict]] = None,   # Fix H
) -> dict:
    n_nodes = G.number_of_nodes()
    n_edges = G.number_of_edges()

    health = compute_health_score(G, contradiction_report, temporal_summary, causal_chains)

    report: dict = {
        "_schema_version": "graphify-enhanced-1.0",
        "_generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "health": health,       # Fix G
        "warnings": warnings or [],  # Fix H
        "summary": {
            "node_count": n_nodes, "edge_count": n_edges,
            "directed": G.is_directed(),
            "community_count": len({d.get("community") for _, d in G.nodes(data=True)
                                    if d.get("community") is not None}),
            "has_temporal_data": (temporal_summary is not None
                                  and not temporal_summary.get("skipped_no_git", True)),
            "repo_root": temporal_summary.get("repo_root") if temporal_summary else None,
        },
        "god_nodes": _top_god_nodes(G),
        "communities": _community_summary(G),
        "edge_types": _edge_type_breakdown(G),
        "confidence_breakdown": _confidence_breakdown(G),
        "surprising_connections": _surprising_connections(G, analysis),
        "suggested_questions": (analysis or {}).get("questions", [])[:5],
    }

    if contradiction_report:
        report["contradictions"] = {
            "total": contradiction_report.get("total", 0),
            "by_type": contradiction_report.get("by_type", {}),
            "exclusive_pairs_used": contradiction_report.get("exclusive_pairs_used", 0),
            "items": contradiction_report.get("contradictions", [])[:20],
        }

    if causal_chains:
        report["causal_chains"] = causal_chains[:10]

    if temporal_summary and not temporal_summary.get("skipped_no_git"):
        report["temporal"] = {
            "nodes_enriched": temporal_summary.get("nodes_enriched", 0),
            "supersedes_edges": temporal_summary.get("supersedes_edges_added", 0),
            "most_active_nodes": _most_active_nodes(G),
        }

    return report


def save_report_json(report: dict, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return path


def print_stats(report: dict) -> None:
    """Fix G: prints health score prominently."""
    s = report.get("summary", {})
    h = report.get("health", {})
    god = report.get("god_nodes", [])
    contradictions = report.get("contradictions", {})
    causal = report.get("causal_chains", [])
    warnings = report.get("warnings", [])

    print("\n📊 graphify-enhanced stats")
    print("─" * 44)
    score = h.get("score", "?")
    grade = h.get("grade", "?")
    grade_emoji = {"A":"🟢","B":"🟡","C":"🟠","D":"🔴","F":"💀"}.get(grade,"⚪")
    print(f"  {grade_emoji} Health score: {score}/100  (grade {grade})")
    if h.get("summary"):
        print(f"     {h['summary']}")
    print("─" * 44)
    print(f"  Nodes:       {s.get('node_count','?'):>6}  ({'directed' if s.get('directed') else 'undirected'})")
    print(f"  Edges:       {s.get('edge_count','?'):>6}")
    print(f"  Communities: {s.get('community_count','?'):>6}")
    if s.get("has_temporal_data"):
        t = report.get("temporal", {})
        print(f"  Git nodes:   {t.get('nodes_enriched',0):>6}")
    print("\n🌟 Top god nodes:")
    for n in god[:5]:
        flag = " ⚠️" if n.get("contradiction") else ""
        print(f"  [{n['degree']:>3}°] {n['label']}{flag}")
    conf = report.get("confidence_breakdown", {})
    print(f"\n🏷  Confidence: EXTRACTED={conf.get('EXTRACTED',0)} "
          f"INFERRED={conf.get('INFERRED',0)} AMBIGUOUS={conf.get('AMBIGUOUS',0)}")
    if contradictions.get("total"):
        print(f"\n⚠️  Contradictions: {contradictions['total']} "
              f"(pairs checked: {contradictions.get('exclusive_pairs_used','?')})")
    if causal:
        print(f"\n🔗 Causal chains: {len(causal)}")
        for c in causal[:3]:
            print(f"  {c['root_label']} → {c['downstream_count']} downstream")
    if warnings:
        print(f"\n🔔 Warnings ({len(warnings)}):")
        for w in warnings[:5]:
            print(f"  [{w.get('module','?')}] {w.get('reason','?')}")
    if qs := report.get("suggested_questions", []):
        print("\n❓ Suggested questions:")
        for q in qs[:3]:
            print(f"  • {q}")
    print()


def format_hook_injection(report_path: Path, token_budget: int = 800) -> str:
    if not report_path.exists():
        return ""
    with report_path.open("r", encoding="utf-8") as f:
        report = json.load(f)
    h = report.get("health", {})
    compact = {
        "graph_summary": report.get("summary", {}),
        "health": {"score": h.get("score"), "grade": h.get("grade"), "summary": h.get("summary")},
        "god_nodes": [{"label": n["label"], "degree": n["degree"]} for n in report.get("god_nodes",[])[:5]],
        "communities": len(report.get("communities",[])),
        "contradictions": report.get("contradictions",{}).get("total",0),
        "causal_chains": len(report.get("causal_chains",[])),
        "warnings": len(report.get("warnings",[])),
        "suggested_questions": report.get("suggested_questions",[])[:3],
    }
    injected = json.dumps(compact, indent=2)
    if len(injected) > token_budget * 4:
        compact["god_nodes"] = compact["god_nodes"][:3]
        compact["suggested_questions"] = compact["suggested_questions"][:1]
        injected = json.dumps(compact, indent=2)
    return f"<!-- graphify-enhanced report.json -->\n{injected}"
