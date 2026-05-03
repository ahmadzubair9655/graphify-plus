"""
causal.py — Enhancement #3: Causal chain extraction

Fix D: smarter target node selection — prioritise nodes with rationale_for
       neighbours, "fix/patch/migration/deprecated/hack" label keywords,
       and recently-added nodes (low age_days) over raw high-degree nodes.
Fix A: directed-graph aware chain traversal.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional
import networkx as nx

_CAUSAL_SYSTEM_PROMPT = """You are a code archaeology expert. Given a node and related context,
identify what CAUSED this node to exist. Focus on:
- Security decisions, CVEs, incidents that mandated this code
- Architectural constraints from other files that required this
- Business requirements or external specifications referenced
- Predecessor code or deprecated patterns this replaced

Return ONLY a JSON object:
{
  "causes": [
    {
      "cause_label": "name or short description",
      "cause_source_file": "file path or null",
      "relation_type": "mandated_by | required_by | response_to | evolved_from",
      "confidence": 0.0-1.0,
      "evidence": "one sentence from the corpus that supports this"
    }
  ]
}
Return empty causes list if no clear causal relationship exists. Max 3 causes per node."""

# Fix D: keywords that signal a node is likely causally interesting
_CAUSAL_INTEREST_KEYWORDS = re.compile(
    r"\b(fix|patch|migration|migrate|deprecated|legacy|workaround|hack|"
    r"refactor|security|cve|incident|hotfix|backport|compat|fallback|"
    r"override|shim|bridge|adapter|wrapper|polyfill)\b",
    re.IGNORECASE,
)


def _build_causal_prompt(node_label: str, node_file: str, context_texts: list[str]) -> str:
    context = "\n\n---\n\n".join(context_texts[:5])
    return (
        f"Node: {node_label!r} (defined in {node_file!r})\n\n"
        f"Corpus context:\n{context}\n\n"
        "What caused this node to exist? Respond with JSON only."
    )


def _parse_causal_response(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    try:
        return json.loads(raw).get("causes", [])
    except (json.JSONDecodeError, AttributeError):
        return []


def _collect_rationale_texts(G: nx.Graph) -> list[str]:
    return [
        f"[{data.get('source_file','')}] {data.get('label','')}"
        for _, data in G.nodes(data=True)
        if (data.get("node_type") == "rationale_for"
            or "rationale" in data.get("label", "").lower())
        and data.get("label")
    ]


# ---------------------------------------------------------------------------
# Fix D: smart target node selection
# ---------------------------------------------------------------------------

def _causal_interest_score(nid: str, G: nx.Graph) -> float:
    """
    Score a node's suitability as a causal extraction target.
    Higher = more likely to have an interesting causal story.
    """
    data = G.nodes[nid]
    label = data.get("label", "")
    score = 0.0

    # Keyword match in label
    if _CAUSAL_INTEREST_KEYWORDS.search(label.replace("_", " ")):
        score += 3.0

    # Has a rationale_for neighbour
    neighbors_fn = G.predecessors if G.is_directed() else G.neighbors
    for nb in neighbors_fn(nid):
        nb_data = G.nodes[nb]
        if nb_data.get("node_type") == "rationale_for":
            score += 2.0
            break

    # Recently added (low age_days) — new code is often reactive
    age_days = data.get("age_days")
    if age_days is not None and age_days < 90:
        score += 1.5

    # Penalise raw god nodes with generic labels (utils, base, config)
    generic = re.compile(r"\b(utils?|base|config|common|helper|misc|shared)\b", re.I)
    if generic.search(label.replace("_", " ")):
        score -= 1.0

    return score


def _select_causal_targets(G: nx.Graph, min_degree: int, max_targets: int) -> list[str]:
    """Fix D: select nodes by causal interest score, not just degree."""
    candidates = [
        (nid, _causal_interest_score(nid, G))
        for nid, deg in G.degree()
        if deg >= min_degree
    ]
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [nid for nid, _ in candidates[:max_targets]]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_causal_chains(
    G: nx.Graph,
    llm_fn: Callable[[str, str], str],
    target_node_ids: Optional[list[str]] = None,
    min_degree: int = 3,
    confidence_threshold: float = 0.65,
) -> dict:
    """Add caused_by edges to G based on LLM provenance tracing."""
    rationale_texts = _collect_rationale_texts(G)

    if target_node_ids is None:
        # Fix D: use interest-score selector instead of raw degree
        target_node_ids = _select_causal_targets(G, min_degree, max_targets=40)

    edges_added = 0
    nodes_analysed = 0
    label_to_id = {
        data.get("label", "").lower(): nid
        for nid, data in G.nodes(data=True)
        if data.get("label")
    }

    # Fix A: use predecessors (directed) or neighbors (undirected) for context
    def _neighbour_texts(nid: str) -> list[str]:
        nb_fn = G.predecessors if G.is_directed() else G.neighbors
        texts = []
        for nb in nb_fn(nid):
            nb_data = G.nodes[nb]
            lbl = nb_data.get("label", "")
            src = nb_data.get("source_file", "")
            if lbl:
                texts.append(f"[{src}] {lbl}")
        return texts

    for nid in target_node_ids:
        if not G.has_node(nid):
            continue
        data = G.nodes[nid]
        label = data.get("label", nid)
        source_file = data.get("source_file", "")
        context = rationale_texts + _neighbour_texts(nid)
        if not context:
            continue

        prompt = _build_causal_prompt(label, source_file, context)
        try:
            raw = llm_fn(_CAUSAL_SYSTEM_PROMPT, prompt)
            causes = _parse_causal_response(raw)
        except Exception:
            causes = []

        for cause in causes:
            conf = float(cause.get("confidence", 0.0))
            if conf < confidence_threshold:
                continue
            cause_label = cause.get("cause_label", "").lower()
            cause_file  = cause.get("cause_source_file")
            relation    = cause.get("relation_type", "caused_by")
            evidence    = cause.get("evidence", "")
            cause_nid   = label_to_id.get(cause_label)

            if cause_nid is None:
                cause_nid = f"cause::{cause_label[:64]}"
                G.add_node(cause_nid,
                           label=cause.get("cause_label", cause_label),
                           source_file=cause_file or "",
                           node_type="causal_source",
                           synthetic=True)
                label_to_id[cause_label] = cause_nid

            G.add_edge(cause_nid, nid,
                       relation=relation, confidence="INFERRED",
                       confidence_score=conf, evidence=evidence, causal_edge=True)
            edges_added += 1
        nodes_analysed += 1

    return {"edges_added": edges_added, "nodes_analysed": nodes_analysed}


def top_causal_chains(G: nx.Graph, top_n: int = 5) -> list[dict]:
    """Return top N causal chains by downstream count. Fix A: uses DiGraph subgraph."""
    causal_relations = {
        "caused_by", "mandated_by", "required_by", "response_to", "evolved_from"
    }
    causal_edges = [
        (u, v, d) for u, v, d in G.edges(data=True)
        if d.get("causal_edge") or d.get("relation") in causal_relations
    ]
    if not causal_edges:
        return []

    cg = nx.DiGraph()
    for u, v, d in causal_edges:
        cg.add_edge(u, v, **d)

    chains = []
    for root in [n for n in cg.nodes() if cg.in_degree(n) == 0]:
        descendants = list(nx.descendants(cg, root))
        try:
            longest = max(
                (nx.shortest_path(cg, root, d) for d in descendants if d != root),
                key=len, default=[root],
            )
        except nx.NetworkXNoPath:
            longest = [root]
        chains.append({
            "root_id": root,
            "root_label": G.nodes[root].get("label", root) if G.has_node(root) else root,
            "downstream_count": len(descendants),
            "chain_length": len(longest),
            "path_labels": [
                G.nodes[n].get("label", n) if G.has_node(n) else n
                for n in longest
            ],
        })

    return sorted(chains, key=lambda x: x["downstream_count"], reverse=True)[:top_n]
