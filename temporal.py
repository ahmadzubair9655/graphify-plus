"""
temporal.py — Enhancement #1: Git-aware temporal edges

Adds first_seen, last_modified, commit_author attributes to every node,
and supersedes edges between nodes where one replaced another.

Fix C: one bulk `git log` call builds a file→commits index;
       per-node lookups are O(1) dict hits instead of O(N) subprocesses.

Drop-in: call enrich_graph_with_temporal(G, repo_root) after build_graph().
"""

from __future__ import annotations

import subprocess
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
import networkx as nx


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: Path) -> str:
    """Run a git command and return stdout. Returns '' on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _build_file_commit_index(repo_root: Path) -> dict[str, list[dict]]:
    """
    Run ONE `git log --name-status` over the whole repo and return a
    dict mapping relative-file-path → list[commit] (oldest first).

    This replaces N per-file `git log --follow` calls with a single subprocess.
    Cost: one git call regardless of corpus size.
    """
    raw = _run_git(
        [
            "log",
            "--name-status",
            "--format=COMMIT\x1f%H\x1f%ae\x1f%aI\x1f%s",
        ],
        cwd=repo_root,
    )
    if not raw:
        return {}

    index: dict[str, list[dict]] = {}
    current_commit: dict = {}

    for line in raw.splitlines():
        if line.startswith("COMMIT\x1f"):
            parts = line.split("\x1f", 4)
            if len(parts) == 5:
                current_commit = {
                    "hash": parts[1],
                    "author": parts[2],
                    "date_iso": parts[3],
                    "subject": parts[4],
                }
        elif line and current_commit:
            # name-status lines: "M\tpath/to/file" or "R\told\tnew"
            cols = line.split("\t")
            status = cols[0][0] if cols else ""
            if status in ("A", "M", "D") and len(cols) >= 2:
                rel_path = cols[1].strip()
                index.setdefault(rel_path, []).append(current_commit)
            elif status == "R" and len(cols) >= 3:
                # rename: associate both old and new paths
                for p in (cols[1].strip(), cols[2].strip()):
                    index.setdefault(p, []).append(current_commit)

    # Reverse each list so oldest commit is first
    return {path: list(reversed(commits)) for path, commits in index.items()}


def _detect_repo_root(start: Path) -> Optional[Path]:
    """Walk up from start to find .git directory."""
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _parse_iso(date_iso: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(date_iso)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Supersedes detection
# ---------------------------------------------------------------------------

_RENAME_KEYWORDS = {
    "rename",
    "refactor",
    "replace",
    "supersede",
    "deprecate",
    "migrate",
    "moved",
    "replaced by",
}


def _detect_supersedes_from_commits(
    G: nx.Graph, commits_by_node: dict[str, list[dict]]
) -> list[tuple[str, str, dict]]:
    """
    Heuristic: if node B's oldest commit subject mentions node A's label,
    emit a supersedes edge A → B.
    Returns list of (old_node_id, new_node_id, edge_attrs).
    """
    label_to_id = {
        data.get("label", ""): nid for nid, data in G.nodes(data=True) if data.get("label")
    }
    edges = []
    for nid, commits in commits_by_node.items():
        if not commits:
            continue
        first_commit = commits[0]
        subject_lower = first_commit["subject"].lower()
        if not any(kw in subject_lower for kw in _RENAME_KEYWORDS):
            continue
        # Look for another node's label mentioned in the commit subject
        for label, old_id in label_to_id.items():
            if old_id == nid:
                continue
            if label.lower() in subject_lower:
                edges.append(
                    (
                        old_id,
                        nid,
                        {
                            "relation": "supersedes",
                            "confidence": "INFERRED",
                            "confidence_score": 0.65,
                            "commit_hash": first_commit["hash"],
                            "commit_subject": first_commit["subject"],
                            "temporal_edge": True,
                        },
                    )
                )
    return edges


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_graph_with_temporal(
    G: nx.Graph,
    corpus_root: Path,
    repo_root: Optional[Path] = None,
) -> dict:
    """
    Enrich G in-place with temporal attributes on nodes and supersedes edges.

    Returns a summary dict:
      {
        "nodes_enriched": int,
        "supersedes_edges_added": int,
        "repo_root": str | None,
        "skipped_no_git": bool,
      }
    """
    corpus_root = Path(corpus_root).resolve()
    if repo_root is None:
        repo_root = _detect_repo_root(corpus_root)

    if repo_root is None:
        # Not a git repo — annotate nodes with a flag and return
        for nid in G.nodes():
            G.nodes[nid]["temporal_enriched"] = False
        return {
            "nodes_enriched": 0,
            "supersedes_edges_added": 0,
            "repo_root": None,
            "skipped_no_git": True,
        }

    # Fix C: one bulk git log → O(1) lookup per node instead of O(N) subprocesses
    file_index = _build_file_commit_index(repo_root)

    commits_by_node: dict[str, list[dict]] = {}
    nodes_enriched = 0

    for nid, data in G.nodes(data=True):
        source_file = data.get("source_file")
        if not source_file:
            continue
        path = Path(source_file)
        try:
            rel = str(path.relative_to(repo_root)) if path.is_absolute() else str(path)
        except ValueError:
            rel = str(path)
        rel = rel.replace("\\", "/")

        commits = file_index.get(rel, [])
        commits_by_node[nid] = commits

        if not commits:
            continue

        first = commits[0]
        last = commits[-1]
        first_dt = _parse_iso(first["date_iso"])
        last_dt = _parse_iso(last["date_iso"])

        G.nodes[nid]["first_seen"] = first["date_iso"]
        G.nodes[nid]["last_modified"] = last["date_iso"]
        G.nodes[nid]["first_author"] = first["author"]
        G.nodes[nid]["last_author"] = last["author"]
        G.nodes[nid]["commit_count"] = len(commits)
        G.nodes[nid]["temporal_enriched"] = True

        # Age in days
        if first_dt and last_dt:
            age_days = (last_dt - first_dt).days
            G.nodes[nid]["age_days"] = age_days

        nodes_enriched += 1

    # Add supersedes edges
    supersedes = _detect_supersedes_from_commits(G, commits_by_node)
    for old_id, new_id, attrs in supersedes:
        if G.has_node(old_id) and G.has_node(new_id):
            G.add_edge(old_id, new_id, **attrs)

    return {
        "nodes_enriched": nodes_enriched,
        "supersedes_edges_added": len(supersedes),
        "repo_root": str(repo_root),
        "skipped_no_git": False,
    }


def get_timeline(G: nx.Graph) -> list[dict]:
    """
    Return a chronological list of nodes sorted by first_seen.
    Useful for report sections and audit trails.
    """
    timeline = []
    for nid, data in G.nodes(data=True):
        if data.get("first_seen"):
            timeline.append(
                {
                    "id": nid,
                    "label": data.get("label", nid),
                    "first_seen": data["first_seen"],
                    "last_modified": data.get("last_modified"),
                    "commit_count": data.get("commit_count", 0),
                    "source_file": data.get("source_file"),
                }
            )
    return sorted(timeline, key=lambda x: x["first_seen"])
