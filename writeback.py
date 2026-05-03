"""
writeback.py — Enhancement #6: MCP write-back and corrections ledger

Fix F: idempotency guard — applying the same correction twice is a no-op;
       conflicting corrections on the same (src,tgt) are surfaced as warnings
       instead of silently overwriting each other.
"""

from __future__ import annotations

import hashlib, json, time
from pathlib import Path
from typing import Any, Optional
import networkx as nx

DEFAULT_CORRECTIONS_FILE = "graphify-out/corrections.jsonl"


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def _record_fingerprint(record: dict) -> str:
    """Stable hash for dedup — excludes timestamp."""
    key = {k: v for k, v in record.items() if k != "timestamp"}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def _append_correction(record: dict, corrections_path: Path) -> None:
    corrections_path.parent.mkdir(parents=True, exist_ok=True)
    with corrections_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_corrections(corrections_path: Path) -> list[dict]:
    if not corrections_path.exists():
        return []
    records = []
    with corrections_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


# ---------------------------------------------------------------------------
# Write tool handlers
# ---------------------------------------------------------------------------

def handle_annotate_node(
    G: nx.Graph, node_id: str, note: str,
    corrections_path: Path, author: str = "assistant",
) -> dict:
    if not G.has_node(node_id):
        return {"ok": False, "error": f"Node '{node_id}' not found in graph."}

    record = {"op": "annotate_node", "node_id": node_id,
              "note": note, "author": author, "timestamp": time.time()}

    # Fix F: idempotency — skip if exact same note already recorded
    existing = _read_corrections(corrections_path)
    fp = _record_fingerprint(record)
    if any(_record_fingerprint(r) == fp for r in existing):
        return {"ok": True, "node_id": node_id, "message": "Annotation already recorded (skipped duplicate).", "duplicate": True}

    _append_correction(record, corrections_path)
    annots = G.nodes[node_id].get("annotations", [])
    if isinstance(annots, str):
        annots = [annots]
    annots.append({"note": note, "author": author, "timestamp": record["timestamp"]})
    G.nodes[node_id]["annotations"] = annots
    return {"ok": True, "node_id": node_id,
            "label": G.nodes[node_id].get("label", node_id),
            "message": f"Annotation added to '{G.nodes[node_id].get('label', node_id)}'."}


def handle_correct_edge(
    G: nx.Graph, source_id: str, target_id: str, new_relation: str,
    corrections_path: Path, old_relation: Optional[str] = None, author: str = "assistant",
) -> dict:
    if not G.has_node(source_id):
        return {"ok": False, "error": f"Source node '{source_id}' not found."}
    if not G.has_node(target_id):
        return {"ok": False, "error": f"Target node '{target_id}' not found."}

    record = {"op": "correct_edge", "source_id": source_id, "target_id": target_id,
              "old_relation": old_relation, "new_relation": new_relation,
              "author": author, "timestamp": time.time()}

    # Fix F: conflict detection — warn if existing correction disagrees
    existing = _read_corrections(corrections_path)
    for r in existing:
        if (r.get("op") == "correct_edge"
                and r.get("source_id") == source_id
                and r.get("target_id") == target_id
                and r.get("new_relation") != new_relation):
            return {
                "ok": False,
                "conflict": True,
                "error": (
                    f"Conflict: a previous correction already set this edge to "
                    f"'{r['new_relation']}' (author: {r.get('author','?')}). "
                    f"Pass old_relation='{r['new_relation']}' to override it explicitly."
                ),
                "existing_relation": r["new_relation"],
            }

    # Idempotency check
    fp = _record_fingerprint(record)
    if any(_record_fingerprint(r) == fp for r in existing):
        return {"ok": True, "message": "Edge correction already recorded (skipped duplicate).", "duplicate": True}

    _append_correction(record, corrections_path)
    edge_exists = G.has_edge(source_id, target_id)
    if edge_exists:
        G[source_id][target_id]["relation"] = new_relation
        G[source_id][target_id]["corrected"] = True
        G[source_id][target_id]["correction_author"] = author
    else:
        G.add_edge(source_id, target_id, relation=new_relation,
                   confidence="EXTRACTED", corrected=True, correction_author=author)

    src_l = G.nodes[source_id].get("label", source_id)
    tgt_l = G.nodes[target_id].get("label", target_id)
    return {"ok": True, "message": f"Edge {src_l!r} --[{new_relation}]--> {tgt_l!r} recorded.",
            "edge_existed": edge_exists}


def handle_flag_ambiguous(
    G: nx.Graph, node_id: str, reason: str,
    corrections_path: Path, author: str = "assistant",
) -> dict:
    if not G.has_node(node_id):
        return {"ok": False, "error": f"Node '{node_id}' not found."}
    record = {"op": "flag_ambiguous", "node_id": node_id,
              "reason": reason, "author": author, "timestamp": time.time()}
    fp = _record_fingerprint(record)
    existing = _read_corrections(corrections_path)
    if any(_record_fingerprint(r) == fp for r in existing):
        return {"ok": True, "message": "Flag already recorded (skipped duplicate).", "duplicate": True}
    _append_correction(record, corrections_path)
    G.nodes[node_id]["flagged_ambiguous"] = True
    G.nodes[node_id]["flag_reason"] = reason
    return {"ok": True, "message": f"Node '{G.nodes[node_id].get('label', node_id)}' flagged: {reason}"}


# ---------------------------------------------------------------------------
# Apply corrections
# ---------------------------------------------------------------------------

def apply_corrections(
    graph_path: Path,
    corrections_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> dict:
    """Apply corrections.jsonl to graph.json. Fix F: idempotent — tracks applied fingerprints."""
    if corrections_path is None:
        corrections_path = graph_path.parent / "corrections.jsonl"
    if output_path is None:
        output_path = graph_path
    if not graph_path.exists():
        return {"ok": False, "error": f"Graph file not found: {graph_path}"}

    with graph_path.open("r", encoding="utf-8") as f:
        graph_data = json.load(f)

    corrections = _read_corrections(corrections_path)
    if not corrections:
        return {"ok": True, "applied": 0, "message": "No corrections to apply."}

    node_by_id = {n["id"]: n for n in graph_data.get("nodes", [])}
    edges = graph_data.get("edges", [])
    applied_fingerprints: set[str] = set(graph_data.get("_applied_corrections", []))

    applied = skipped = conflicts = 0

    for rec in corrections:
        fp = _record_fingerprint(rec)
        if fp in applied_fingerprints:
            skipped += 1
            continue

        op = rec.get("op")
        if op == "annotate_node":
            nid = rec["node_id"]
            if nid in node_by_id:
                annots = node_by_id[nid].get("annotations", [])
                annots.append({"note": rec["note"], "author": rec.get("author",""),
                               "timestamp": rec.get("timestamp")})
                node_by_id[nid]["annotations"] = annots
                applied_fingerprints.add(fp)
                applied += 1
            else:
                skipped += 1

        elif op == "correct_edge":
            # Fix F: detect conflict with already-applied corrections
            matched = False
            for edge in edges:
                if (edge.get("source") == rec["source_id"]
                        and edge.get("target") == rec["target_id"]):
                    existing_rel = edge.get("relation","")
                    if rec.get("old_relation") and existing_rel != rec["old_relation"]:
                        conflicts += 1
                        break
                    edge["relation"] = rec["new_relation"]
                    edge["corrected"] = True
                    edge["correction_author"] = rec.get("author","")
                    matched = True
                    applied_fingerprints.add(fp)
                    applied += 1
                    break
            if not matched and conflicts == 0:
                edges.append({"source": rec["source_id"], "target": rec["target_id"],
                              "relation": rec["new_relation"], "confidence": "EXTRACTED",
                              "corrected": True, "correction_author": rec.get("author","")})
                applied_fingerprints.add(fp)
                applied += 1

        elif op == "flag_ambiguous":
            nid = rec["node_id"]
            if nid in node_by_id:
                node_by_id[nid]["flagged_ambiguous"] = True
                node_by_id[nid]["flag_reason"] = rec.get("reason","")
                applied_fingerprints.add(fp)
                applied += 1
            else:
                skipped += 1

    graph_data["nodes"] = list(node_by_id.values())
    graph_data["edges"] = edges
    graph_data["_applied_corrections"] = list(applied_fingerprints)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(graph_data, f, indent=2, ensure_ascii=False)

    return {"ok": True, "applied": applied, "skipped": skipped,
            "conflicts": conflicts,
            "corrections_file": str(corrections_path),
            "output_file": str(output_path)}


# ---------------------------------------------------------------------------
# MCP tool schemas
# ---------------------------------------------------------------------------

WRITE_TOOL_SCHEMAS = [
    {"name": "annotate_node",
     "description": "Add a note or correction to a graph node.",
     "input_schema": {"type": "object",
                      "properties": {"node_id": {"type": "string"},
                                     "note": {"type": "string"}},
                      "required": ["node_id", "note"]}},
    {"name": "correct_edge",
     "description": "Correct or add an edge between two nodes.",
     "input_schema": {"type": "object",
                      "properties": {"source_id": {"type": "string"},
                                     "target_id": {"type": "string"},
                                     "new_relation": {"type": "string"},
                                     "old_relation": {"type": "string"}},
                      "required": ["source_id", "target_id", "new_relation"]}},
    {"name": "flag_ambiguous",
     "description": "Flag a node as ambiguous or needing human review.",
     "input_schema": {"type": "object",
                      "properties": {"node_id": {"type": "string"},
                                     "reason": {"type": "string"}},
                      "required": ["node_id", "reason"]}},
]


def handle_write_tool(
    tool_name: str, tool_input: dict[str, Any],
    G: nx.Graph, corrections_path: Path,
) -> dict:
    if tool_name == "annotate_node":
        return handle_annotate_node(G, tool_input["node_id"], tool_input["note"], corrections_path)
    elif tool_name == "correct_edge":
        return handle_correct_edge(G, tool_input["source_id"], tool_input["target_id"],
                                   tool_input["new_relation"], corrections_path,
                                   old_relation=tool_input.get("old_relation"))
    elif tool_name == "flag_ambiguous":
        return handle_flag_ambiguous(G, tool_input["node_id"], tool_input.get("reason",""), corrections_path)
    return {"ok": False, "error": f"Unknown write tool: {tool_name}"}
