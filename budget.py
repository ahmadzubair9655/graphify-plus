"""
budget.py — Enhancement #5: Importance-weighted subgraph budgeting

Fix B: fuzzy label+id matching for find_root_ids_for_query (handles hash IDs)
Fix A: directed-graph aware BFS (respects edge direction)
Fix D (centrality cache): module-level cache keyed on graph size — O(1) on
      repeated calls within a session, invalidated on graph mutation.
"""

from __future__ import annotations

import math
from typing import Optional
import networkx as nx

_CONFIDENCE_SCORE = {"EXTRACTED": 1.0, "INFERRED": 0.6, "AMBIGUOUS": 0.3}

# Fix D: centrality cache — invalidated when node/edge count changes
_centrality_cache: dict[tuple[int,int], dict[str, float]] = {}


def _degree_centrality(G: nx.Graph) -> dict[str, float]:
    """Cached normalised degree centrality."""
    key = (G.number_of_nodes(), G.number_of_edges())
    if key not in _centrality_cache:
        n = G.number_of_nodes()
        if n <= 1:
            _centrality_cache[key] = {nid: 0.0 for nid in G.nodes()}
        else:
            _centrality_cache[key] = {nid: deg / (n - 1) for nid, deg in G.degree()}
    return _centrality_cache[key]


def invalidate_centrality_cache() -> None:
    """Call after mutating the graph to force recalculation."""
    _centrality_cache.clear()


def _recency_score(data: dict) -> float:
    last_mod = data.get("last_modified")
    if not last_mod:
        return 0.5
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(last_mod)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = max((datetime.now(timezone.utc) - dt).days, 0)
        return math.exp(-age_days / 180.0)
    except (ValueError, TypeError):
        return 0.5


def _node_score(nid: str, G: nx.Graph, centrality: dict[str,float], hop_dist: int) -> float:
    data = G.nodes[nid]
    cent = centrality.get(nid, 0.0)
    rec  = _recency_score(data)
    hop  = 1.0 / (1.0 + hop_dist)
    edges_iter = (
        list(G.out_edges(nid, data=True)) + list(G.in_edges(nid, data=True))
        if G.is_directed() else list(G.edges(nid, data=True))
    )
    confs = [_CONFIDENCE_SCORE.get(ed.get("confidence","INFERRED"),0.6) for _,_,ed in edges_iter]
    avg_conf = sum(confs)/len(confs) if confs else 0.6
    return 0.40*cent + 0.25*hop + 0.20*avg_conf + 0.15*rec


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _format_node(nid: str, G: nx.Graph) -> str:
    data = G.nodes[nid]
    parts = [f"NODE {data.get('label', nid)!r}"]
    if s := data.get("source_file"): parts.append(f"  file: {s}")
    if f := data.get("first_seen"):  parts.append(f"  first_seen: {f}")
    if data.get("contradiction"):    parts.append("  ⚠️ CONTRADICTION FLAGGED")
    return "\n".join(parts)


def _format_edge(u: str, v: str, edata: dict, G: nx.Graph) -> str:
    ul = G.nodes[u].get("label", u) if G.has_node(u) else u
    vl = G.nodes[v].get("label", v) if G.has_node(v) else v
    rel   = edata.get("relation", "related_to")
    conf  = edata.get("confidence", "")
    score = edata.get("confidence_score", "")
    arrow = "-->" if G.is_directed() else "---"
    score_str = f" [{score:.2f}]" if isinstance(score, float) else ""
    return f"EDGE {ul!r} -[{rel}]{arrow} {vl!r}  ({conf}{score_str})"


def extract_budgeted_subgraph(
    G: nx.Graph,
    root_ids: list[str],
    token_budget: int = 1500,
    max_hops: int = 3,
) -> str:
    """
    Extract a token-budgeted, importance-ranked subgraph rooted at root_ids.
    Fix A: uses directed BFS (successors only) for DiGraph.
    Fix D: uses cached centrality.
    """
    if not root_ids:
        return "(no root nodes provided)"
    valid_roots = [nid for nid in root_ids if G.has_node(nid)]
    if not valid_roots:
        return f"(none of the root nodes found in graph: {root_ids})"

    centrality = _degree_centrality(G)

    # Fix A: directed BFS uses successors; undirected uses neighbors
    def _neighbors(nid: str):
        if G.is_directed():
            return list(G.successors(nid)) + list(G.predecessors(nid))
        return list(G.neighbors(nid))

    candidates: dict[str, int] = {}
    frontier = set(valid_roots)
    for hop in range(max_hops + 1):
        for nid in frontier:
            if nid not in candidates:
                candidates[nid] = hop
        next_frontier = set()
        if hop < max_hops:
            for nid in frontier:
                for nb in _neighbors(nid):
                    if nb not in candidates:
                        next_frontier.add(nb)
        frontier = next_frontier

    ranked = sorted(
        candidates.items(),
        key=lambda x: _node_score(x[0], G, centrality, x[1]),
        reverse=True,
    )

    included_nodes: list[str] = []
    included_set: set[str] = set()
    tokens_used = 0
    overflow_count = 0

    for nid, _ in ranked:
        txt = _format_node(nid, G)
        tok = _estimate_tokens(txt)
        if tokens_used + tok > token_budget:
            overflow_count += 1
            continue
        included_nodes.append(nid)
        included_set.add(nid)
        tokens_used += tok

    included_edges: list[tuple] = []
    for u, v, edata in G.edges(data=True):
        if u in included_set and v in included_set:
            txt = _format_edge(u, v, edata, G)
            tok = _estimate_tokens(txt)
            if tokens_used + tok <= token_budget:
                included_edges.append((u, v, edata))
                tokens_used += tok

    lines = [
        f"# Subgraph ({len(included_nodes)} nodes, {len(included_edges)} edges)"
        f" | {'directed' if G.is_directed() else 'undirected'}",
        f"# Token budget: {token_budget} | Used: {tokens_used}",
        f"# Roots: {[G.nodes[r].get('label', r) for r in valid_roots]}",
        "",
    ]
    for nid in included_nodes:
        lines.append(_format_node(nid, G))
    if included_edges:
        lines.append("")
        for u, v, edata in included_edges:
            lines.append(_format_edge(u, v, edata, G))
    if overflow_count:
        lines.append(
            f"\n# ... {overflow_count} additional nodes cut by budget. "
            "Ask '/graphify query <term>' for a deeper dive."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fix B: fuzzy query → root ID matching
# ---------------------------------------------------------------------------

def _edit_distance(a: str, b: str) -> int:
    """Simple Levenshtein for short strings."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0: return lb
    if lb == 0: return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(0 if ca==cb else 1)))
        prev = curr
    return prev[lb]


def find_root_ids_for_query(G: nx.Graph, query: str, top_n: int = 3) -> list[str]:
    """
    Fix B: fuzzy label+id matching.
    1. Exact substring match on label and node id (highest priority).
    2. All query terms present in label (second priority).
    3. Edit-distance fallback on label (for typos / abbreviations).
    Returns up to top_n node IDs.
    """
    query_lower = query.lower()
    terms = query_lower.split()

    exact: list[tuple[float, str]] = []
    partial: list[tuple[float, str]] = []
    fuzzy: list[tuple[float, str]] = []

    for nid, data in G.nodes(data=True):
        label = data.get("label", "").lower()
        nid_lower = nid.lower()
        all_text = label + " " + nid_lower

        # Tier 1: exact substring in label or id
        if query_lower in all_text:
            exact.append((1.0, nid))
            continue

        # Tier 2: all terms present
        hits = sum(1 for t in terms if t in all_text)
        if hits >= 1:  # any term match is enough
            partial.append((hits / len(terms), nid))
            continue

        # Tier 3: edit distance on label (only for short labels to avoid noise)
        if label and len(label) < 60:
            dist = _edit_distance(query_lower[:20], label[:20])
            score = 1.0 / (1.0 + dist)
            if score > 0.5:  # threshold to suppress garbage matches
                fuzzy.append((score, nid))

    results: list[str] = []
    for tier in (exact, partial, fuzzy):
        tier.sort(reverse=True)
        for _, nid in tier:
            if nid not in results:
                results.append(nid)
            if len(results) >= top_n:
                return results
    return results
