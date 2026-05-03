"""
reconcile.py — Enhancement #2: Cross-repo semantic stitching

Fix B (namespace auto-tagging): auto_tag_namespaces() infers namespace from
source_file path topology instead of requiring manual tagging.
"""

from __future__ import annotations

import hashlib, json
from pathlib import Path
from typing import Callable, Optional
import networkx as nx


def _node_fingerprint(data: dict) -> str:
    key = f"{data.get('label','')}::{data.get('source_file','')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Fix B: auto namespace detection from source_file paths
# ---------------------------------------------------------------------------

def auto_tag_namespaces(G: nx.Graph, out_dir: Optional[Path] = None) -> dict:
    """
    Infer namespace from source_file path topology and tag G nodes in-place.

    Strategy:
    - Collect all source_file values.
    - Find the longest common prefix across all files.
    - Everything after the common prefix is a relative path.
    - The first directory component of that relative path becomes the namespace
      (e.g., "repo-A/src/auth.py" → namespace "repo-A").
    - Falls back to reading graphify-out/_namespaces.json if it exists (written
      by `graphify merge-graphs`).

    Returns: {namespace: node_count} summary.
    """
    # Try loading namespace map from merge metadata
    if out_dir:
        ns_file = Path(out_dir) / "_namespaces.json"
        if ns_file.exists():
            try:
                ns_map = json.loads(ns_file.read_text())
                for nid, data in G.nodes(data=True):
                    src = data.get("source_file", "")
                    for prefix, ns in ns_map.items():
                        if src.startswith(prefix):
                            G.nodes[nid]["namespace"] = ns
                            break
                summary: dict[str, int] = {}
                for _, d in G.nodes(data=True):
                    ns = d.get("namespace", "__default__")
                    summary[ns] = summary.get(ns, 0) + 1
                return summary
            except (json.JSONDecodeError, OSError):
                pass

    # Path-topology approach
    source_files = [
        data.get("source_file", "")
        for _, data in G.nodes(data=True)
        if data.get("source_file")
    ]
    if not source_files:
        return {}

    # Find common prefix at directory boundary
    parts_list = [Path(f).parts for f in source_files]
    min_len = min(len(p) for p in parts_list)
    common_depth = 0
    for i in range(min_len):
        if len({p[i] for p in parts_list}) == 1:
            common_depth = i + 1
        else:
            break

    summary: dict[str, int] = {}
    for nid, data in G.nodes(data=True):
        src = data.get("source_file", "")
        if not src:
            continue
        parts = Path(src).parts
        remaining = parts[common_depth:]
        # namespace = first remaining component (repo root dir or top-level package)
        ns = remaining[0] if remaining else "__default__"
        G.nodes[nid]["namespace"] = ns
        summary[ns] = summary.get(ns, 0) + 1

    return summary


def _group_by_namespace(G: nx.Graph) -> dict[str, list[tuple[str, dict]]]:
    groups: dict[str, list] = {}
    for nid, data in G.nodes(data=True):
        ns = data.get("namespace", "__default__")
        groups.setdefault(ns, []).append((nid, data))
    return groups


def _candidate_pairs(groups, max_pairs=200):
    namespaces = [ns for ns in groups if ns != "__default__"]
    if len(namespaces) < 2:
        return []
    pairs, seen = [], set()
    for i, ns_a in enumerate(namespaces):
        for ns_b in namespaces[i+1:]:
            for nid_a, data_a in groups[ns_a]:
                label_a = data_a.get("label","")
                prefix_a = label_a.split()[0].lower() if label_a else ""
                for nid_b, data_b in groups[ns_b]:
                    label_b = data_b.get("label","")
                    prefix_b = label_b.split()[0].lower() if label_b else ""
                    if prefix_a and prefix_a == prefix_b:
                        key = tuple(sorted([nid_a, nid_b]))
                        if key not in seen:
                            seen.add(key)
                            pairs.append(((nid_a, data_a), (nid_b, data_b)))
                            if len(pairs) >= max_pairs:
                                return pairs
    return pairs


def _build_comparison_prompt(pairs):
    items = [
        f"[{i}] A: label={da.get('label')!r} ns={da.get('namespace')!r} file={da.get('source_file')!r}\n"
        f"    B: label={db.get('label')!r} ns={db.get('namespace')!r} file={db.get('source_file')!r}"
        for i, ((_, da), (_, db)) in enumerate(pairs)
    ]
    return (
        "You are a code knowledge graph analyst. For each pair, decide if A and B represent "
        "the SAME concept/function/class across different repositories.\n\n"
        'Respond ONLY with a JSON array. Each item: {"pair_index": int, "equivalent": bool, '
        '"confidence": float 0-1, "reason": str}\n\n' + "\n".join(items)
    )


def _parse_llm_response(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def reconcile_merged_graph(
    G: nx.Graph,
    llm_fn: Optional[Callable[[str], str]] = None,
    batch_size: int = 20,
    confidence_threshold: float = 0.70,
    auto_tag: bool = True,
    out_dir: Optional[Path] = None,
) -> dict:
    """
    Find cross-namespace semantic duplicates and add cross_repo_equivalent_to edges.

    Fix B: auto_tag=True (default) calls auto_tag_namespaces() before grouping,
           so you don't have to tag namespaces manually.
    """
    ns_summary = {}
    if auto_tag:
        ns_summary = auto_tag_namespaces(G, out_dir=out_dir)

    groups = _group_by_namespace(G)
    pairs = _candidate_pairs(groups)

    if not pairs:
        return {"edges_added": 0, "pairs_evaluated": 0,
                "skipped_no_llm": llm_fn is None, "namespace_summary": ns_summary}

    edges_added = pairs_evaluated = 0

    if llm_fn is None:
        for (nid_a, da), (nid_b, db) in pairs:
            la = da.get("label","").lower().strip()
            lb = db.get("label","").lower().strip()
            if la and la == lb:
                G.add_edge(nid_a, nid_b, relation="cross_repo_equivalent_to",
                           confidence="INFERRED", confidence_score=0.60,
                           method="label_equality_heuristic")
                edges_added += 1
        return {"edges_added": edges_added, "pairs_evaluated": len(pairs),
                "skipped_no_llm": True, "namespace_summary": ns_summary}

    for start in range(0, len(pairs), batch_size):
        batch = pairs[start:start+batch_size]
        prompt = _build_comparison_prompt(batch)
        try:
            results = _parse_llm_response(llm_fn(prompt))
        except Exception:
            results = []
        for result in results:
            idx = result.get("pair_index")
            if idx is None or idx >= len(batch):
                continue
            if not result.get("equivalent"):
                continue
            conf = float(result.get("confidence", 0.0))
            if conf < confidence_threshold:
                continue
            (nid_a, _), (nid_b, _) = batch[idx]
            G.add_edge(nid_a, nid_b, relation="cross_repo_equivalent_to",
                       confidence="INFERRED", confidence_score=conf,
                       reason=result.get("reason",""), method="llm_comparison")
            edges_added += 1
        pairs_evaluated += len(batch)

    return {"edges_added": edges_added, "pairs_evaluated": pairs_evaluated,
            "skipped_no_llm": False, "namespace_summary": ns_summary}
