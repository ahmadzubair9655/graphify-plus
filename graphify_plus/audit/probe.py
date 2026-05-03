"""
graphify_plus.audit.probe — Adversarial graph audit (production-grade).

The audit answers a different question than every other graph-quality tool:
not "what did I find" but "what should you not trust about what I found."

7 probes:
    1. edge_deletion_stability — random edge deletion vs community partition
    2. confidence_drift — variance of INFERRED edge confidence scores
    3. rename_sensitivity — nodes whose label is referenced by their own edges
    4. structural_fragility — articulation points (cut vertices)
    5. lonely_inferred_edges — INFERRED edges with no structural corroboration
    6. modularity_quality — Newman modularity score for the community partition
    7. centrality_drift — disagreement between degree, betweenness, pagerank
"""

from __future__ import annotations

import math
import random
from typing import Any

import networkx as nx

GradeStr = str  # "A" | "B" | "C" | "D" | "F" | "N/A"

_GRADE_TO_NUMERIC: dict[GradeStr, int] = {
    "A": 4,
    "B": 3,
    "C": 2,
    "D": 1,
    "F": 0,
    "N/A": -1,
}

_DEFAULT_TOP_K = 15
_DEFAULT_SAMPLE_SEED = 42


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_graph(G: Any) -> tuple[bool, str]:
    """Return (ok, reason). Every probe calls this first."""
    if G is None:
        return False, "graph is None"
    if not hasattr(G, "nodes") or not hasattr(G, "edges"):
        return False, f"object {type(G).__name__} is not a NetworkX graph"
    if G.number_of_nodes() == 0:
        return False, "graph has no nodes"
    return True, ""


def _safe_node_label(G: nx.Graph, nid: Any) -> str:
    if not G.has_node(nid):
        return str(nid)
    return str(G.nodes[nid].get("label", nid))


def _grade_from_numeric(score: float, thresholds: tuple = (0.9, 0.75, 0.6, 0.4)) -> GradeStr:
    """Map a 0-1 score to a letter grade. NaN-safe."""
    if score != score:
        return "N/A"
    a, b, c, d = thresholds
    if score >= a:
        return "A"
    if score >= b:
        return "B"
    if score >= c:
        return "C"
    if score >= d:
        return "D"
    return "F"


# ---------------------------------------------------------------------------
# Community helpers
# ---------------------------------------------------------------------------


def _community_set(G: nx.Graph) -> dict[Any, set]:
    out: dict[Any, set] = {}
    for nid, data in G.nodes(data=True):
        c = data.get("community")
        if c is not None:
            out.setdefault(c, set()).add(nid)
    return out


def _community_overlap(a: dict[Any, set], b: dict[Any, set]) -> float:
    if not a or not b:
        return 0.0
    a_sets = [s for s in a.values() if s]
    if not a_sets:
        return 0.0
    total = 0.0
    for sa in a_sets:
        best = 0.0
        for sb in b.values():
            if not sb:
                continue
            union = len(sa | sb)
            if union == 0:
                continue
            best = max(best, len(sa & sb) / union)
        total += best
    return total / len(a_sets)


# ---------------------------------------------------------------------------
# Probe 1: edge deletion stability
# ---------------------------------------------------------------------------


def probe_edge_deletion_stability(
    G: nx.Graph,
    deletion_rate: float = 0.05,
    iterations: int = 5,
    seed: int = _DEFAULT_SAMPLE_SEED,
) -> dict:
    ok, reason = _validate_graph(G)
    if not ok:
        return {"skipped": True, "reason": reason}
    if not (0.0 < deletion_rate < 1.0):
        return {"skipped": True, "reason": f"invalid deletion_rate {deletion_rate}"}
    if iterations < 1:
        return {"skipped": True, "reason": f"invalid iterations {iterations}"}

    original = _community_set(G)
    if not original:
        return {"skipped": True, "reason": "no community attribute on nodes"}

    edges = list(G.edges())
    if not edges:
        return {"skipped": True, "reason": "graph has no edges"}

    n_to_delete = max(1, int(len(edges) * deletion_rate))
    if n_to_delete >= len(edges):
        return {"skipped": True, "reason": "deletion_rate too high — would delete all edges"}

    rng = random.Random(seed)
    overlaps = []
    for _ in range(iterations):
        H = G.copy()
        to_remove = rng.sample(edges, n_to_delete)
        H.remove_edges_from(to_remove)
        overlaps.append(_community_overlap(original, _community_set(H)))

    avg = sum(overlaps) / len(overlaps)
    return {
        "skipped": False,
        "deletion_rate": deletion_rate,
        "iterations": iterations,
        "edges_deleted_per_iter": n_to_delete,
        "avg_overlap": round(avg, 4),
        "min_overlap": round(min(overlaps), 4),
        "max_overlap": round(max(overlaps), 4),
        "stability_grade": _grade_from_numeric(avg),
        "interpretation": (
            f"After deleting {int(deletion_rate * 100)}% of edges across {iterations} runs, "
            f"{int(avg * 100)}% of community structure was preserved on average."
        ),
    }


# ---------------------------------------------------------------------------
# Probe 2: confidence drift
# ---------------------------------------------------------------------------


def probe_confidence_drift(G: nx.Graph) -> dict:
    ok, reason = _validate_graph(G)
    if not ok:
        return {"skipped": True, "reason": reason}

    scores: list[float] = []
    for _, _, data in G.edges(data=True):
        if data.get("confidence") != "INFERRED":
            continue
        s = data.get("confidence_score")
        if isinstance(s, (int, float)) and 0.0 <= s <= 1.0:
            scores.append(float(s))

    if len(scores) < 3:
        return {
            "skipped": True,
            "reason": f"need >=3 INFERRED edges with confidence_score (got {len(scores)})",
        }

    n = len(scores)
    mean = sum(scores) / n
    if mean == 0:
        return {"skipped": True, "reason": "all confidence scores are 0"}

    var = sum((s - mean) ** 2 for s in scores) / n
    stddev = math.sqrt(var)
    cv = stddev / mean

    grade_score = max(0.0, 1.0 - cv * 2.5)
    return {
        "skipped": False,
        "inferred_edge_count": n,
        "mean_confidence": round(mean, 4),
        "stddev": round(stddev, 4),
        "coefficient_of_variation": round(cv, 4),
        "drift_grade": _grade_from_numeric(grade_score),
        "interpretation": (
            f"INFERRED edges have mean confidence {mean:.2f} +/- {stddev:.2f} "
            f"(CV={cv:.2f}). "
            + (
                "Model is consistent."
                if cv < 0.15
                else "Some inconsistency in model confidence."
                if cv < 0.30
                else "High uncertainty - consider re-running extraction."
            )
        ),
    }


# ---------------------------------------------------------------------------
# Probe 3: rename sensitivity
# ---------------------------------------------------------------------------


def probe_rename_sensitivity(
    G: nx.Graph,
    top_k: int = 20,
    min_label_length: int = 3,
) -> dict:
    ok, reason = _validate_graph(G)
    if not ok:
        return {"skipped": True, "reason": reason}

    fragile_nodes: list[dict] = []
    high_degree = sorted(G.degree(), key=lambda x: x[1], reverse=True)[:top_k]

    for nid, deg in high_degree:
        if deg < 1:
            continue
        label = _safe_node_label(G, nid)
        if len(label) < min_label_length:
            continue

        if G.is_directed():
            edge_iter = list(G.out_edges(nid, data=True)) + list(G.in_edges(nid, data=True))
        else:
            edge_iter = list(G.edges(nid, data=True))

        ref_count = 0
        label_lower = label.lower()
        for _, _, edata in edge_iter:
            evidence_text = " ".join(
                str(edata.get(field, "") or "")
                for field in ("evidence", "description", "reason", "commit_subject")
            ).lower()
            if label_lower in evidence_text:
                ref_count += 1

        if ref_count > 0:
            fragile_nodes.append(
                {
                    "id": str(nid),
                    "label": label,
                    "degree": deg,
                    "label_referencing_edges": ref_count,
                    "fragility_pct": round(ref_count / max(deg, 1) * 100, 1),
                }
            )

    fragile_nodes.sort(key=lambda x: x["fragility_pct"], reverse=True)

    grade = "A"
    if fragile_nodes:
        worst = max(f["fragility_pct"] for f in fragile_nodes) / 100
        grade = _grade_from_numeric(1.0 - worst, thresholds=(0.85, 0.7, 0.5, 0.3))

    return {
        "skipped": False,
        "fragile_count": len(fragile_nodes),
        "fragile_nodes": fragile_nodes[:10],
        "rename_grade": grade,
        "interpretation": (
            f"{len(fragile_nodes)} high-degree node(s) have edges that reference "
            "their label in evidence/description. Renaming these breaks attribution."
        ),
    }


# ---------------------------------------------------------------------------
# Probe 4: structural fragility
# ---------------------------------------------------------------------------


def probe_structural_fragility(G: nx.Graph, top_k: int = 15) -> dict:
    ok, reason = _validate_graph(G)
    if not ok:
        return {"skipped": True, "reason": reason}

    H = G.to_undirected() if G.is_directed() else G
    if H.is_multigraph():
        H = nx.Graph(H)

    if H.number_of_nodes() < 2:
        return {"skipped": True, "reason": "graph too small for articulation analysis"}

    try:
        if not nx.is_connected(H):
            largest_cc = max(nx.connected_components(H), key=len)
            H = H.subgraph(largest_cc).copy()
        cuts = list(nx.articulation_points(H))
    except (nx.NetworkXError, ValueError) as exc:
        return {"skipped": True, "reason": f"NetworkX error: {exc}"}
    except Exception as exc:
        return {"skipped": True, "reason": f"unexpected error: {exc}"}

    points = []
    for nid in cuts:
        points.append(
            {
                "id": str(nid),
                "label": _safe_node_label(G, nid),
                "degree": G.degree(nid),
                "source_file": str(G.nodes[nid].get("source_file", "")) if G.has_node(nid) else "",
            }
        )
    points.sort(key=lambda x: x["degree"], reverse=True)

    cut_ratio = len(cuts) / H.number_of_nodes() if H.number_of_nodes() else 0
    grade = _grade_from_numeric(1.0 - cut_ratio, thresholds=(0.95, 0.85, 0.7, 0.5))

    return {
        "skipped": False,
        "articulation_count": len(cuts),
        "cut_ratio_pct": round(cut_ratio * 100, 2),
        "articulation_points": points[:top_k],
        "structural_grade": grade,
        "interpretation": (
            f"{len(cuts)} cut vertices ({cut_ratio * 100:.1f}% of nodes). "
            + (
                "Few bottlenecks - graph is robust."
                if cut_ratio < 0.05
                else "Moderate bottlenecks."
                if cut_ratio < 0.15
                else "Many bottlenecks - consider adding redundant edges."
            )
        ),
    }


# ---------------------------------------------------------------------------
# Probe 5: lonely INFERRED edges
# ---------------------------------------------------------------------------


def probe_lonely_inferred_edges(
    G: nx.Graph,
    high_confidence_cutoff: float = 0.85,
    top_k: int = 15,
) -> dict:
    ok, reason = _validate_graph(G)
    if not ok:
        return {"skipped": True, "reason": reason}

    lonely: list[dict] = []
    total_inferred = 0

    def _neighbors(nid: Any) -> set:
        if G.is_directed():
            return set(G.successors(nid)) | set(G.predecessors(nid))
        return set(G.neighbors(nid))

    for u, v, data in G.edges(data=True):
        if data.get("confidence") != "INFERRED":
            continue
        total_inferred += 1
        score = data.get("confidence_score", 1.0)
        if isinstance(score, (int, float)) and score >= high_confidence_cutoff:
            continue

        u_n = _neighbors(u)
        u_n.discard(v)
        v_n = _neighbors(v)
        v_n.discard(u)

        if not (u_n & v_n):
            lonely.append(
                {
                    "source": str(u),
                    "target": str(v),
                    "source_label": _safe_node_label(G, u),
                    "target_label": _safe_node_label(G, v),
                    "relation": str(data.get("relation", "")),
                    "confidence_score": float(score) if isinstance(score, (int, float)) else None,
                }
            )

    lonely.sort(key=lambda x: x.get("confidence_score") or 0.0)

    if total_inferred:
        ratio = len(lonely) / total_inferred
        grade = _grade_from_numeric(1.0 - ratio, thresholds=(0.9, 0.75, 0.5, 0.3))
    else:
        grade = "N/A"

    return {
        "skipped": False,
        "total_inferred_edges": total_inferred,
        "lonely_count": len(lonely),
        "lonely_ratio_pct": round(len(lonely) / max(total_inferred, 1) * 100, 2),
        "lonely_edges": lonely[:top_k],
        "lonely_grade": grade,
        "interpretation": (
            f"{len(lonely)} of {total_inferred} INFERRED edges have no shared neighbour. "
            "These have no structural corroboration."
        ),
    }


# ---------------------------------------------------------------------------
# Probe 6: modularity quality
# ---------------------------------------------------------------------------


def probe_modularity_quality(G: nx.Graph) -> dict:
    ok, reason = _validate_graph(G)
    if not ok:
        return {"skipped": True, "reason": reason}

    communities = _community_set(G)
    if not communities:
        return {"skipped": True, "reason": "no community attribute on nodes"}
    if len(communities) < 2:
        return {"skipped": True, "reason": "need at least 2 communities"}

    H = G.to_undirected() if G.is_directed() else G
    if H.is_multigraph():
        H = nx.Graph(H)
    if H.number_of_edges() == 0:
        return {"skipped": True, "reason": "no edges"}

    try:
        comm_lists = [list(s) for s in communities.values() if s]
        Q = nx.community.modularity(H, comm_lists)
    except (nx.NetworkXError, ZeroDivisionError, ValueError) as exc:
        return {"skipped": True, "reason": f"modularity error: {exc}"}

    if Q >= 0.4:
        grade = "A"
    elif Q >= 0.3:
        grade = "B"
    elif Q >= 0.15:
        grade = "C"
    elif Q >= 0.0:
        grade = "D"
    else:
        grade = "F"

    return {
        "skipped": False,
        "modularity_Q": round(Q, 4),
        "community_count": len(communities),
        "modularity_grade": grade,
        "interpretation": (
            f"Modularity Q={Q:.3f}. "
            + (
                "Strong community structure."
                if Q >= 0.4
                else "Moderate community structure."
                if Q >= 0.3
                else "Weak community structure - partition may not reflect real groupings."
                if Q >= 0.0
                else "Worse than random - partition is wrong."
            )
        ),
    }


# ---------------------------------------------------------------------------
# Probe 7: centrality drift
# ---------------------------------------------------------------------------


def probe_centrality_drift(G: nx.Graph, top_k: int = 10) -> dict:
    ok, reason = _validate_graph(G)
    if not ok:
        return {"skipped": True, "reason": reason}

    if G.number_of_nodes() > 10_000:
        return {
            "skipped": True,
            "reason": "graph too large for betweenness centrality (>10k nodes)",
        }

    if G.number_of_edges() == 0:
        return {"skipped": True, "reason": "no edges"}

    H = G.to_undirected() if G.is_directed() else G
    if H.is_multigraph():
        H = nx.Graph(H)

    try:
        deg = dict(H.degree())
        try:
            bet = nx.betweenness_centrality(H, normalized=True)
        except Exception:
            bet = {}
        try:
            pr = nx.pagerank(H)
        except (nx.PowerIterationFailedConvergence, ZeroDivisionError):
            pr = {}
    except Exception as exc:
        return {"skipped": True, "reason": f"centrality error: {exc}"}

    def _top(d: dict, k: int) -> list:
        return [n for n, _ in sorted(d.items(), key=lambda x: x[1], reverse=True)[:k]]

    deg_top = set(_top(deg, top_k))
    bet_top = set(_top(bet, top_k)) if bet else set()
    pr_top = set(_top(pr, top_k)) if pr else set()

    def _jac(a: set, b: set) -> float:
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    overlaps = {
        "degree_vs_betweenness": _jac(deg_top, bet_top) if bet else None,
        "degree_vs_pagerank": _jac(deg_top, pr_top) if pr else None,
        "betweenness_vs_pagerank": _jac(bet_top, pr_top) if (bet and pr) else None,
    }
    valid = [v for v in overlaps.values() if v is not None]
    avg_overlap = sum(valid) / len(valid) if valid else None

    grade = _grade_from_numeric(avg_overlap) if avg_overlap is not None else "N/A"

    return {
        "skipped": False,
        "top_k": top_k,
        "overlaps": {k: round(v, 4) if v is not None else None for k, v in overlaps.items()},
        "avg_overlap": round(avg_overlap, 4) if avg_overlap is not None else None,
        "centrality_grade": grade,
        "interpretation": (
            f"Top-{top_k} agreement across centrality metrics: "
            + (
                f"{int(avg_overlap * 100)}% - "
                + (
                    "metrics agree, god nodes are robust."
                    if avg_overlap >= 0.7
                    else "moderate agreement - some metric-dependence."
                    if avg_overlap >= 0.4
                    else "metrics disagree - be skeptical of single-metric god-node lists."
                )
                if avg_overlap is not None
                else "could not compute"
            )
        ),
    }


# ---------------------------------------------------------------------------
# Audit aggregation
# ---------------------------------------------------------------------------


def _aggregate_grade(probe_results: dict[str, dict]) -> dict:
    grade_keys = [
        ("edge_deletion_stability", "stability_grade", 0.20),
        ("confidence_drift", "drift_grade", 0.15),
        ("rename_sensitivity", "rename_grade", 0.10),
        ("structural_fragility", "structural_grade", 0.20),
        ("lonely_inferred_edges", "lonely_grade", 0.15),
        ("modularity_quality", "modularity_grade", 0.10),
        ("centrality_drift", "centrality_grade", 0.10),
    ]

    weighted_sum = 0.0
    weight_total = 0.0
    grades_present: dict[str, str] = {}

    for probe_key, grade_field, weight in grade_keys:
        result = probe_results.get(probe_key, {})
        if result.get("skipped"):
            continue
        g = result.get(grade_field)
        if g and g != "N/A":
            num = _GRADE_TO_NUMERIC.get(g, -1)
            if num >= 0:
                weighted_sum += num * weight
                weight_total += weight
                grades_present[probe_key] = g

    if weight_total == 0:
        return {"overall_grade": "N/A", "components": {}, "weighted_score": None}

    avg = weighted_sum / weight_total
    if avg >= 3.5:
        overall = "A"
    elif avg >= 2.5:
        overall = "B"
    elif avg >= 1.5:
        overall = "C"
    elif avg >= 0.5:
        overall = "D"
    else:
        overall = "F"

    return {
        "overall_grade": overall,
        "weighted_score": round(avg, 3),
        "components": grades_present,
    }


def run_audit(
    G: nx.Graph,
    iterations: int = 5,
    deletion_rate: float = 0.05,
    seed: int = _DEFAULT_SAMPLE_SEED,
    skip_expensive: bool = False,
    only: list[str] | None = None,
) -> dict:
    ok, reason = _validate_graph(G)
    if not ok:
        return {
            "_schema": "graphify-plus-audit-1.1",
            "skipped": True,
            "reason": reason,
            "graph_size": {"nodes": 0, "edges": 0},
            "overall_grade": "N/A",
            "warnings": [{"probe": "init", "reason": reason}],
        }

    all_probes = {
        "edge_deletion_stability": lambda: probe_edge_deletion_stability(
            G,
            deletion_rate=deletion_rate,
            iterations=iterations,
            seed=seed,
        ),
        "confidence_drift": lambda: probe_confidence_drift(G),
        "rename_sensitivity": lambda: probe_rename_sensitivity(G),
        "structural_fragility": lambda: probe_structural_fragility(G),
        "lonely_inferred_edges": lambda: probe_lonely_inferred_edges(G),
        "modularity_quality": lambda: probe_modularity_quality(G),
        "centrality_drift": lambda: probe_centrality_drift(G),
    }

    if only:
        all_probes = {k: v for k, v in all_probes.items() if k in only}
    if skip_expensive:
        all_probes.pop("centrality_drift", None)

    results: dict = {}
    warnings: list[dict] = []

    for name, runner in all_probes.items():
        try:
            results[name] = runner()
        except Exception as exc:
            warnings.append({"probe": name, "reason": f"unhandled exception: {exc}"})
            results[name] = {"skipped": True, "reason": str(exc)}

    aggregate = _aggregate_grade(results)

    return {
        "_schema": "graphify-plus-audit-1.1",
        "graph_size": {
            "nodes": G.number_of_nodes(),
            "edges": G.number_of_edges(),
            "directed": G.is_directed(),
            "multigraph": G.is_multigraph(),
        },
        "overall_grade": aggregate["overall_grade"],
        "weighted_score": aggregate["weighted_score"],
        "component_grades": aggregate["components"],
        **results,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Renderers and CI helpers
# ---------------------------------------------------------------------------


def format_audit_report(audit: dict) -> str:
    if audit.get("skipped"):
        return f"# Audit\n\nSkipped: {audit.get('reason', 'unknown')}\n"

    lines = ["# Adversarial Audit Report\n"]
    s = audit.get("graph_size", {})
    direction = "directed" if s.get("directed") else "undirected"
    lines.append(
        f"**Graph:** {s.get('nodes', '?')} nodes, {s.get('edges', '?')} edges "
        f"({direction}{', multigraph' if s.get('multigraph') else ''})\n"
    )

    overall = audit.get("overall_grade", "N/A")
    score = audit.get("weighted_score")
    grade_emoji = {"A": "[A]", "B": "[B]", "C": "[C]", "D": "[D]", "F": "[F]", "N/A": "[?]"}.get(
        overall, "[?]"
    )
    lines.append(
        f"## {grade_emoji} Overall: **{overall}** "
        f"({f'weighted score {score}' if score is not None else 'no scoreable probes'})\n"
    )

    sections = [
        ("edge_deletion_stability", "1. Edge deletion stability", "stability_grade"),
        ("confidence_drift", "2. Confidence drift", "drift_grade"),
        ("rename_sensitivity", "3. Rename sensitivity", "rename_grade"),
        ("structural_fragility", "4. Structural fragility", "structural_grade"),
        ("lonely_inferred_edges", "5. Lonely INFERRED edges", "lonely_grade"),
        ("modularity_quality", "6. Modularity quality", "modularity_grade"),
        ("centrality_drift", "7. Centrality drift", "centrality_grade"),
    ]

    for key, title, grade_field in sections:
        result = audit.get(key, {})
        if result.get("skipped"):
            lines.append(f"## {title}\n_skipped: {result.get('reason', 'unknown')}_\n")
            continue
        grade = result.get(grade_field, "?")
        lines.append(f"## {title} - Grade {grade}")
        interp = result.get("interpretation", "")
        if interp:
            lines.append(f"_{interp}_\n")

        if key == "rename_sensitivity":
            for n in result.get("fragile_nodes", [])[:5]:
                lines.append(
                    f"- **{n['label']}**: {n['label_referencing_edges']}/{n['degree']} "
                    f"edges reference its label ({n['fragility_pct']}% fragile)"
                )
        elif key == "structural_fragility":
            for p in result.get("articulation_points", [])[:5]:
                lines.append(f"- **{p['label']}** ({p['degree']} deg)")
        elif key == "lonely_inferred_edges":
            for e in result.get("lonely_edges", [])[:5]:
                score_s = (
                    f"{e['confidence_score']:.2f}" if e.get("confidence_score") is not None else "?"
                )
                lines.append(
                    f"- {e['source_label']} -[{e['relation']}]-> {e['target_label']} "
                    f"(score: {score_s})"
                )
        if result.get("interpretation"):
            lines.append("")

    warnings = audit.get("warnings", [])
    if warnings:
        lines.append("## Warnings")
        for w in warnings:
            lines.append(f"- [{w.get('probe')}] {w.get('reason')}")

    return "\n".join(lines)


def audit_meets_threshold(audit: dict, min_grade: GradeStr) -> bool:
    """For CI: returns True if overall_grade >= min_grade. N/A always passes."""
    overall = audit.get("overall_grade", "N/A")
    if overall == "N/A":
        return True
    return _GRADE_TO_NUMERIC.get(overall, -1) >= _GRADE_TO_NUMERIC.get(min_grade, 0)
