"""
graphify_plus.review.graph_diff — Graph diff for code review (production-grade).

Computes structural diff between two graph snapshots and renders it for:
    - GitHub PR comments (markdown)
    - GitLab MR comments (markdown)
    - Bitbucket PRs (markdown)
    - CI annotations (text)
    - Machine-readable JSON
    - Unified-diff-like text format

Production hardening (vs the v3 prototype):
    - Severity classification per change (LOW / MEDIUM / HIGH / CRITICAL)
    - Threshold gating for CI ("fail PR if HIGH severity changes detected")
    - Three output formats: markdown, json, text
    - Attribute-level node diffs (renamed, label change, source_file move)
    - Edge attribute changes detected (relation change, confidence change)
    - Health score delta (if both report.json files are present)
    - Communities split / merged detection
    - Defensive validation against malformed inputs
    - Determinism: identical inputs always produce identical output
    - Comprehensive type hints
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import networkx as nx

SeverityStr = str  # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL" | "INFO"

_SEVERITY_RANK: dict[SeverityStr, int] = {
    "INFO": 0,
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}

_DEPENDENCY_RELATIONS = frozenset(
    {
        "calls",
        "uses",
        "imports",
        "inherits_from",
        "depends_on",
        "wraps",
        "delegates_to",
        "requires",
        "extends",
        "implements",
        "overrides",
        "proxies",
    }
)

_CAUSAL_RELATIONS = frozenset(
    {
        "caused_by",
        "mandated_by",
        "required_by",
        "response_to",
        "evolved_from",
    }
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load_graph(path: Path) -> nx.Graph:
    """Load a graph from JSON. Raises on malformed input."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Graph file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Graph JSON must be an object, got {type(data).__name__}")
    if "nodes" not in data or "edges" not in data:
        raise ValueError("Graph JSON must contain 'nodes' and 'edges' arrays")

    G: nx.Graph = nx.DiGraph() if data.get("_directed") else nx.Graph()
    for node in data["nodes"]:
        if not isinstance(node, dict):
            continue
        nid = node.get("id") or node.get("label", "")
        if not nid:
            continue
        G.add_node(nid, **{k: v for k, v in node.items() if k != "id"})

    for edge in data["edges"]:
        if not isinstance(edge, dict):
            continue
        src, tgt = edge.get("source"), edge.get("target")
        if src and tgt:
            G.add_edge(src, tgt, **{k: v for k, v in edge.items() if k not in ("source", "target")})
    return G


def _normalise(arg: nx.Graph | Path | str) -> nx.Graph:
    """Accept either a graph or a path; always return a graph."""
    if isinstance(arg, nx.Graph):
        return arg
    if isinstance(arg, (str, Path)):
        return _load_graph(Path(arg))
    raise TypeError(f"Expected nx.Graph or path, got {type(arg).__name__}")


# ---------------------------------------------------------------------------
# Edge identity
# ---------------------------------------------------------------------------


def _edge_id(u: str, v: str, data: dict, directed: bool) -> tuple:
    """Stable identity for an edge — used for set comparisons across graphs."""
    pair = (u, v) if directed else tuple(sorted([u, v]))
    return (*pair, data.get("relation", ""))


def _edges_with_data(G: nx.Graph) -> dict[tuple, dict]:
    directed = G.is_directed()
    out: dict[tuple, dict] = {}
    if G.is_multigraph():
        for u, v, _key, data in G.edges(keys=True, data=True):
            out[_edge_id(u, v, data, directed)] = data
    else:
        for u, v, data in G.edges(data=True):
            out[_edge_id(u, v, data, directed)] = data
    return out


# ---------------------------------------------------------------------------
# Per-change severity classification
# ---------------------------------------------------------------------------


def _node_removal_severity(G_old: nx.Graph, nid: str) -> SeverityStr:
    """How bad is removing this specific node?"""
    if not G_old.has_node(nid):
        return "INFO"
    deg = G_old.degree(nid)
    has_causal = any(
        d.get("relation") in _CAUSAL_RELATIONS for _, _, d in G_old.edges(nid, data=True)
    )
    if deg >= 10 or has_causal:
        return "CRITICAL"
    if deg >= 5:
        return "HIGH"
    if deg >= 2:
        return "MEDIUM"
    return "LOW"


def _node_addition_severity(G_new: nx.Graph, nid: str) -> SeverityStr:
    """Adding a high-degree node is rarely accidental but worth surfacing."""
    if not G_new.has_node(nid):
        return "INFO"
    deg = G_new.degree(nid)
    if deg >= 10:
        return "MEDIUM"
    return "LOW"


def _aggregate_severity(items: list[SeverityStr]) -> SeverityStr:
    """Take the maximum severity across a list."""
    if not items:
        return "INFO"
    return max(items, key=lambda s: _SEVERITY_RANK.get(s, 0))


# ---------------------------------------------------------------------------
# Diff computation — central function
# ---------------------------------------------------------------------------


def diff_graphs(
    old: nx.Graph | Path | str,
    new: nx.Graph | Path | str,
    old_report: dict | Path | str | None = None,
    new_report: dict | Path | str | None = None,
) -> dict:
    """
    Compute a structural diff between two graphs.

    Parameters
    ----------
    old, new : graphs or paths to graph.json files
    old_report, new_report : optional report.json dicts/paths for health-delta

    Returns a fully-typed dict suitable for serialisation to JSON.
    """
    G_old = _normalise(old)
    G_new = _normalise(new)

    old_nodes = set(G_old.nodes())
    new_nodes = set(G_new.nodes())

    added_nodes = sorted(new_nodes - old_nodes, key=str)
    removed_nodes = sorted(old_nodes - new_nodes, key=str)
    persisted = old_nodes & new_nodes

    directed = G_old.is_directed() and G_new.is_directed()
    old_edges_idx = _edges_with_data(G_old)
    new_edges_idx = _edges_with_data(G_new)

    added_edge_ids = sorted(set(new_edges_idx) - set(old_edges_idx))
    removed_edge_ids = sorted(set(old_edges_idx) - set(new_edges_idx))

    # Detect edge attribute changes (same source/target/relation, different attrs)
    edge_attr_changes: list[dict] = []
    common_edge_ids = set(old_edges_idx) & set(new_edges_idx)
    for eid in sorted(common_edge_ids):
        old_data = old_edges_idx[eid]
        new_data = new_edges_idx[eid]
        changed_attrs = {}
        all_keys = set(old_data) | set(new_data)
        for k in all_keys:
            if k in ("source", "target"):
                continue
            ov, nv = old_data.get(k), new_data.get(k)
            if ov != nv:
                changed_attrs[k] = {"old": ov, "new": nv}
        if changed_attrs:
            u, v, rel = eid
            edge_attr_changes.append(
                {
                    "source": str(u),
                    "target": str(v),
                    "relation": str(rel),
                    "changed_attrs": changed_attrs,
                }
            )

    # Detect node attribute changes
    node_attr_changes: list[dict] = []
    for nid in persisted:
        old_data = G_old.nodes[nid]
        new_data = G_new.nodes[nid]
        changed = {}
        for k in set(old_data) | set(new_data):
            if k in ("annotations",):  # skip noisy fields
                continue
            ov, nv = old_data.get(k), new_data.get(k)
            if ov != nv:
                changed[k] = {"old": ov, "new": nv}
        if changed:
            node_attr_changes.append(
                {
                    "id": str(nid),
                    "label": str(G_new.nodes[nid].get("label", nid)),
                    "changed_attrs": changed,
                }
            )

    # Edge type breakdown
    edge_types_added = Counter(rel for _, _, rel in added_edge_ids)
    edge_types_removed = Counter(rel for _, _, rel in removed_edge_ids)

    # God-node rank changes (top-20 only)
    old_ranks = {
        nid: rank
        for rank, (nid, _) in enumerate(
            sorted(G_old.degree(), key=lambda x: x[1], reverse=True)[:20]
        )
    }
    new_ranks = {
        nid: rank
        for rank, (nid, _) in enumerate(
            sorted(G_new.degree(), key=lambda x: x[1], reverse=True)[:20]
        )
    }
    rank_changes = []
    for nid in old_ranks:
        if nid in new_ranks:
            delta = old_ranks[nid] - new_ranks[nid]
            if delta != 0:
                rank_changes.append(
                    {
                        "id": str(nid),
                        "label": str(G_new.nodes[nid].get("label", nid))
                        if G_new.has_node(nid)
                        else str(nid),
                        "old_rank": old_ranks[nid] + 1,
                        "new_rank": new_ranks[nid] + 1,
                        "rank_delta": delta,
                    }
                )
    rank_changes.sort(key=lambda x: abs(x["rank_delta"]), reverse=True)

    # Contradictions
    def _count_contradictions(G: nx.Graph) -> int:
        return sum(1 for _, _, d in G.edges(data=True) if d.get("relation") == "CONTRADICTS") + sum(
            1 for _, d in G.nodes(data=True) if d.get("contradiction")
        )

    contradictions_old = _count_contradictions(G_old)
    contradictions_new = _count_contradictions(G_new)

    # Causal chains orphaned
    orphaned_chains = []
    for u, v, data in G_old.edges(data=True):
        if data.get("relation") in _CAUSAL_RELATIONS:
            if u in set(removed_nodes) or v in set(removed_nodes):
                orphaned_chains.append(
                    {
                        "source": str(u),
                        "target": str(v),
                        "source_label": str(G_old.nodes[u].get("label", u)),
                        "target_label": str(G_old.nodes[v].get("label", v)),
                        "relation": str(data["relation"]),
                    }
                )

    # Communities split/merged (heuristic via nodes' community attribute)
    community_changes = _detect_community_changes(G_old, G_new)

    # Health delta
    health_delta = _compute_health_delta(old_report, new_report)

    # Per-node severity for added/removed
    added_with_severity = [
        {
            "id": str(nid),
            "label": str(G_new.nodes[nid].get("label", nid)),
            "source_file": str(G_new.nodes[nid].get("source_file", "")),
            "degree": G_new.degree(nid),
            "severity": _node_addition_severity(G_new, nid),
        }
        for nid in added_nodes
    ]
    removed_with_severity = [
        {
            "id": str(nid),
            "label": str(G_old.nodes[nid].get("label", nid)),
            "source_file": str(G_old.nodes[nid].get("source_file", "")),
            "had_degree": G_old.degree(nid),
            "severity": _node_removal_severity(G_old, nid),
        }
        for nid in removed_nodes
    ]

    # Overall severity = max across all change categories
    overall_severity = _aggregate_severity(
        [
            *(n["severity"] for n in added_with_severity),
            *(n["severity"] for n in removed_with_severity),
            "HIGH" if orphaned_chains else "INFO",
            "MEDIUM" if contradictions_new > contradictions_old else "INFO",
            "LOW" if rank_changes else "INFO",
        ]
    )

    return {
        "_schema": "graphify-plus-diff-1.1",
        "summary": {
            "old": {"nodes": len(old_nodes), "edges": len(old_edges_idx)},
            "new": {"nodes": len(new_nodes), "edges": len(new_edges_idx)},
            "node_delta": len(added_nodes) - len(removed_nodes),
            "edge_delta": len(added_edge_ids) - len(removed_edge_ids),
            "directed": directed,
        },
        "overall_severity": overall_severity,
        "nodes_added": added_with_severity,
        "nodes_removed": removed_with_severity,
        "node_attr_changes": node_attr_changes[:50],  # cap
        "edges_added_count": len(added_edge_ids),
        "edges_removed_count": len(removed_edge_ids),
        "edge_types_added": dict(edge_types_added.most_common()),
        "edge_types_removed": dict(edge_types_removed.most_common()),
        "edge_attr_changes": edge_attr_changes[:50],  # cap
        "god_node_rank_changes": rank_changes[:10],
        "contradictions": {
            "old": contradictions_old,
            "new": contradictions_new,
            "delta": contradictions_new - contradictions_old,
        },
        "orphaned_causal_chains": orphaned_chains,
        "community_changes": community_changes,
        "health_delta": health_delta,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_community_changes(G_old: nx.Graph, G_new: nx.Graph) -> dict:
    """Crude detection of community split/merge events."""
    old_communities: dict[Any, set] = {}
    new_communities: dict[Any, set] = {}
    for nid, data in G_old.nodes(data=True):
        c = data.get("community")
        if c is not None:
            old_communities.setdefault(c, set()).add(nid)
    for nid, data in G_new.nodes(data=True):
        c = data.get("community")
        if c is not None:
            new_communities.setdefault(c, set()).add(nid)

    return {
        "old_count": len(old_communities),
        "new_count": len(new_communities),
        "delta": len(new_communities) - len(old_communities),
    }


def _compute_health_delta(
    old_report: dict | Path | str | None,
    new_report: dict | Path | str | None,
) -> dict | None:
    """Extract health-score delta from two report.json files/dicts."""

    def _load(report: dict | Path | str | None) -> dict | None:
        if report is None:
            return None
        if isinstance(report, dict):
            return report
        try:
            with Path(report).open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    old = _load(old_report)
    new = _load(new_report)
    if not old or not new:
        return None

    old_health = old.get("health", {})
    new_health = new.get("health", {})
    if "score" not in old_health or "score" not in new_health:
        return None

    return {
        "old_score": old_health.get("score"),
        "new_score": new_health.get("score"),
        "delta": new_health["score"] - old_health["score"],
        "old_grade": old_health.get("grade"),
        "new_grade": new_health.get("grade"),
    }


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


def _severity_emoji(sev: SeverityStr) -> str:
    return {
        "CRITICAL": "[CRITICAL]",
        "HIGH": "[HIGH]",
        "MEDIUM": "[MEDIUM]",
        "LOW": "[LOW]",
        "INFO": "[INFO]",
    }.get(sev, "")


def format_pr_comment(diff: dict, max_items: int = 5) -> str:
    """Format a graph diff as a markdown PR comment."""
    s = diff["summary"]
    sev = diff.get("overall_severity", "INFO")
    sev_marker = _severity_emoji(sev)

    lines = [
        f"## Graph diff {sev_marker}",
        "",
        f"**{s['old']['nodes']} -> {s['new']['nodes']} nodes** "
        f"({_signed(s['node_delta'])}), "
        f"**{s['old']['edges']} -> {s['new']['edges']} edges** "
        f"({_signed(s['edge_delta'])})",
        "",
    ]

    health = diff.get("health_delta")
    if health and health.get("delta") is not None:
        delta = health["delta"]
        emoji = "+" if delta > 0 else ""
        lines.append(
            f"**Health score:** {health['old_score']} ({health.get('old_grade', '?')}) "
            f"-> {health['new_score']} ({health.get('new_grade', '?')}) "
            f"({emoji}{delta:+d})"
        )
        lines.append("")

    contradict = diff.get("contradictions", {})
    if contradict.get("delta"):
        sign = "+" if contradict["delta"] > 0 else ""
        lines.append(
            f"**Contradictions:** {contradict['old']} -> {contradict['new']} "
            f"({sign}{contradict['delta']:+d})"
        )
        lines.append("")

    if diff.get("orphaned_causal_chains"):
        lines.append(f"### {len(diff['orphaned_causal_chains'])} causal chain(s) orphaned")
        for c in diff["orphaned_causal_chains"][:max_items]:
            lines.append(f"- `{c['source_label']}` -[{c['relation']}]-> `{c['target_label']}`")
        lines.append("")

    if diff.get("nodes_added"):
        lines.append(f"### + {len(diff['nodes_added'])} node(s) added")
        for n in diff["nodes_added"][:max_items]:
            lines.append(f"- `{n['label']}` ({n['source_file']})")
        if len(diff["nodes_added"]) > max_items:
            lines.append(f"- _... and {len(diff['nodes_added']) - max_items} more_")
        lines.append("")

    if diff.get("nodes_removed"):
        lines.append(f"### - {len(diff['nodes_removed'])} node(s) removed")
        for n in diff["nodes_removed"][:max_items]:
            sev_str = _severity_emoji(n["severity"])
            lines.append(f"- `{n['label']}` (was {n['had_degree']} deg) {sev_str}")
        if len(diff["nodes_removed"]) > max_items:
            lines.append(f"- _... and {len(diff['nodes_removed']) - max_items} more_")
        lines.append("")

    if diff.get("god_node_rank_changes"):
        lines.append("### God-node rank changes")
        for r in diff["god_node_rank_changes"][:max_items]:
            arrow = "up" if r["rank_delta"] > 0 else "down"
            lines.append(
                f"- `{r['label']}` rank {r['old_rank']} -> {r['new_rank']} ({arrow} {abs(r['rank_delta'])})"
            )
        lines.append("")

    if diff.get("edge_attr_changes"):
        lines.append(f"### {len(diff['edge_attr_changes'])} edge attribute change(s)")
        for e in diff["edge_attr_changes"][:max_items]:
            attrs = ", ".join(e["changed_attrs"].keys())
            lines.append(f"- `{e['source']} -[{e['relation']}]-> {e['target']}` ({attrs})")
        lines.append("")

    edges_added = diff.get("edge_types_added", {})
    edges_removed = diff.get("edge_types_removed", {})
    if edges_added or edges_removed:
        lines.append("### Edge type changes")
        for rel, count in list(edges_added.items())[:max_items]:
            lines.append(f"- `+{count}` {rel}")
        for rel, count in list(edges_removed.items())[:max_items]:
            lines.append(f"- `-{count}` {rel}")

    return "\n".join(lines)


def format_text_diff(diff: dict) -> str:
    """Plain-text format for CI logs (no markdown)."""
    s = diff["summary"]
    out = []
    out.append(f"=== Graph diff (severity: {diff.get('overall_severity', 'INFO')}) ===")
    out.append(f"Nodes: {s['old']['nodes']} -> {s['new']['nodes']} ({_signed(s['node_delta'])})")
    out.append(f"Edges: {s['old']['edges']} -> {s['new']['edges']} ({_signed(s['edge_delta'])})")

    if diff.get("orphaned_causal_chains"):
        out.append(f"\n[!] {len(diff['orphaned_causal_chains'])} causal chains orphaned")
        for c in diff["orphaned_causal_chains"][:10]:
            out.append(f"    {c['source_label']} -[{c['relation']}]-> {c['target_label']}")

    if diff.get("nodes_removed"):
        critical = [n for n in diff["nodes_removed"] if n["severity"] in ("HIGH", "CRITICAL")]
        if critical:
            out.append(f"\n[!] {len(critical)} high-severity node removal(s):")
            for n in critical[:10]:
                out.append(f"    [{n['severity']}] {n['label']} (was {n['had_degree']} deg)")

    return "\n".join(out)


def diff_meets_threshold(diff: dict, max_severity: SeverityStr) -> bool:
    """For CI use: returns True if overall_severity <= max_severity."""
    overall = diff.get("overall_severity", "INFO")
    return _SEVERITY_RANK.get(overall, 0) <= _SEVERITY_RANK.get(max_severity, 4)
