"""
contradict.py — Enhancement #4: Contradiction detection

Fix E: dynamic exclusive-pair detection from actual graph relations instead
       of a hardcoded 4-pair list that misses every real graphify relation type.
Fix A: directed-graph awareness — normalised edge pairs for undirected graphs.
"""

from __future__ import annotations

import re
from typing import Optional
import networkx as nx

_IDEMPOTENT_CLAIMS = re.compile(
    r"\b(idempotent|pure|no[- ]side[- ]effect|read[- ]only|immutable|stateless)\b",
    re.IGNORECASE,
)
_WRITE_RELATIONS = frozenset({
    "writes_to", "modifies", "mutates", "updates", "deletes",
    "persists", "inserts", "overwrites", "truncates",
})
_READ_THEN_WRITE = frozenset({"reads_then_writes", "side_effect"})

_STATIC_EXCLUSIVE_PAIRS = [
    ("calls", "never_calls"),
    ("imports", "does_not_import"),
    ("inherits_from", "unrelated_to"),
    ("uses", "avoids"),
    ("depends_on", "independent_of"),
    ("extends", "unrelated_to"),
    ("wraps", "avoids"),
    ("delegates_to", "independent_of"),
    ("overrides", "unrelated_to"),
    ("proxies", "independent_of"),
    ("mirrors", "diverges_from"),
    ("implements", "violates"),
]

_POSITIVE_PREFIXES = (
    "uses", "calls", "imports", "depends", "wraps",
    "delegates", "extends", "inherits", "implements",
    "mirrors", "overrides", "proxies", "requires",
)


def _has_idempotency_claim(G: nx.Graph, nid: str) -> bool:
    data = G.nodes[nid]
    return bool(_IDEMPOTENT_CLAIMS.search(
        data.get("label", "") + " " + data.get("docstring", "")
    ))


def _has_write_edges(G: nx.Graph, nid: str) -> bool:
    if G.is_directed():
        edges = list(G.out_edges(nid, data=True)) + list(G.in_edges(nid, data=True))
    else:
        edges = list(G.edges(nid, data=True))
    for _, _, edata in edges:
        if edata.get("relation") in _WRITE_RELATIONS | _READ_THEN_WRITE:
            return True
    return False


def _infer_exclusive_pairs(G: nx.Graph) -> list[tuple[str, str]]:
    """Build exclusive pairs dynamically from the actual relations in G (Fix E)."""
    present = {
        edata.get("relation", "")
        for _, _, edata in G.edges(data=True)
        if edata.get("relation")
    }
    pairs = list(_STATIC_EXCLUSIVE_PAIRS)
    seen = {frozenset(p) for p in pairs}

    for rel in present:
        rl = rel.lower()
        if not any(rl.startswith(p) for p in _POSITIVE_PREFIXES):
            continue
        for neg in (f"never_{rel}", f"does_not_{rel}", f"avoids_{rel}",
                    "independent_of", "unrelated_to"):
            if neg in present:
                key = frozenset([rel, neg])
                if key not in seen:
                    seen.add(key)
                    pairs.append((rel, neg))
    return pairs


def _build_edge_index(G: nx.Graph) -> dict[tuple, list[dict]]:
    """Index edges. Fix A: normalise undirected pairs so (a,b)==(b,a)."""
    index: dict[tuple, list[dict]] = {}
    directed = G.is_directed()
    edge_iter = (
        G.edges(keys=True, data=True) if G.is_multigraph()
        else ((u, v, None, d) for u, v, d in G.edges(data=True))
    )
    for u, v, _k, data in edge_iter:
        pair = (u, v) if directed else tuple(sorted([u, v]))
        index.setdefault(pair, []).append(data)
    return index


def _find_relation_contradictions(
    G: nx.Graph,
    edge_index: dict[tuple, list[dict]],
    exclusive_pairs: list[tuple[str, str]],
) -> list[dict]:
    contradictions = []
    for (src, tgt), edge_list in edge_index.items():
        extracted_rels = {e["relation"] for e in edge_list if e.get("confidence") == "EXTRACTED"}
        inferred_rels  = {e["relation"] for e in edge_list if e.get("confidence") == "INFERRED"}
        if not extracted_rels or not inferred_rels:
            continue
        for a_rel, b_rel in exclusive_pairs:
            for ext, inf in [(a_rel, b_rel), (b_rel, a_rel)]:
                if ext in extracted_rels and inf in inferred_rels:
                    contradictions.append({
                        "source_id": src, "target_id": tgt,
                        "source_label": G.nodes[src].get("label", src) if G.has_node(src) else src,
                        "target_label": G.nodes[tgt].get("label", tgt) if G.has_node(tgt) else tgt,
                        "type": "relation_conflict",
                        "extracted_relation": ext, "inferred_relation": inf,
                        "severity": "high",
                        "description": f"AST says '{ext}' but LLM inferred '{inf}' for same pair.",
                    })
                    break
    return contradictions


def _find_docstring_contradictions(G: nx.Graph) -> list[dict]:
    return [
        {
            "source_id": nid, "target_id": None,
            "source_label": G.nodes[nid].get("label", nid),
            "target_label": None,
            "type": "docstring_vs_implementation", "severity": "medium",
            "description": "Node claims idempotency/purity but AST detected write/mutate edges.",
        }
        for nid in G.nodes()
        if _has_idempotency_claim(G, nid) and _has_write_edges(G, nid)
    ]


def detect_contradictions(
    G: nx.Graph,
    annotate_graph: bool = True,
    extra_exclusive_pairs: Optional[list[tuple[str, str]]] = None,
) -> dict:
    """
    Find contradictions in G (directed or undirected) and optionally annotate.

    extra_exclusive_pairs: user-supplied additional [(rel_a, rel_b), ...] pairs.
    """
    exclusive_pairs = _infer_exclusive_pairs(G)
    if extra_exclusive_pairs:
        exclusive_pairs.extend(extra_exclusive_pairs)

    edge_index = _build_edge_index(G)
    rel_contradictions  = _find_relation_contradictions(G, edge_index, exclusive_pairs)
    doc_contradictions  = _find_docstring_contradictions(G)
    all_contradictions  = rel_contradictions + doc_contradictions
    edges_added = 0

    if annotate_graph:
        for c in rel_contradictions:
            src, tgt = c["source_id"], c["target_id"]
            if G.has_node(src) and tgt and G.has_node(tgt):
                G.add_edge(src, tgt, relation="CONTRADICTS", confidence="AMBIGUOUS",
                           contradiction_type=c["type"], severity=c["severity"],
                           description=c["description"])
                edges_added += 1
        for c in doc_contradictions:
            nid = c["source_id"]
            if G.has_node(nid):
                G.nodes[nid]["contradiction"] = True
                G.nodes[nid]["contradiction_description"] = c["description"]

    return {
        "contradictions": all_contradictions,
        "total": len(all_contradictions),
        "by_type": {
            "relation_conflict": len(rel_contradictions),
            "docstring_vs_implementation": len(doc_contradictions),
        },
        "edges_added": edges_added,
        "exclusive_pairs_used": len(exclusive_pairs),
    }


def format_contradiction_report(contradictions: list[dict]) -> str:
    if not contradictions:
        return "## ⚠️ Contradictions\n\nNone detected.\n"
    lines = ["## ⚠️ Contradictions\n"]
    for severity, emoji, label in [("high","🔴","High"), ("medium","🟡","Medium")]:
        group = [c for c in contradictions if c.get("severity") == severity]
        if not group:
            continue
        lines.append(f"### {emoji} {label} severity ({len(group)})\n")
        for c in group:
            src = c["source_label"]
            tgt = c.get("target_label") or "(self)"
            lines.append(f"- **{src}** ↔ **{tgt}**: {c['description']}")
        lines.append("")
    return "\n".join(lines)
