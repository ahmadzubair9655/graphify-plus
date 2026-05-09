"""Layer 11.1 — shared team graph (local filesystem tier).

The full master plan envisions a small self-hosted server. The
local-tier MVP ships *here*: a shared filesystem path (NFS / Dropbox /
git-backed dir) holds team-wide annotations, decisions, and queries.
Each developer's daemon writes incrementally; reads answer locally
first with shared fallback. Conflict resolution is the existing
``writeback.py`` per-target ``flag_ambiguous`` flow.

Layout::

    <team_root>/
      annotations.jsonl     # append-only stream of team annotations
      shared-queries/       # GPL queries shared across the team
      manifest.json         # team metadata (name, members)

Privacy: only what the user explicitly writes goes here. Source code,
symbol names, snippets are *never* synced.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("graphify_plus.daemon.team")

ANNOTATIONS_FILE = "annotations.jsonl"
QUERIES_DIR = "shared-queries"
MANIFEST_FILE = "manifest.json"


@dataclass
class TeamConfig:
    root: Path
    name: str = "team"
    members: list[str] = field(default_factory=list)


def detect_team_root() -> Path | None:
    """Read ``GRAPHIFY_PLUS_TEAM_ROOT`` env var, else None."""
    raw = os.environ.get("GRAPHIFY_PLUS_TEAM_ROOT")
    return Path(raw).expanduser() if raw else None


def init_team(root: Path, name: str = "team") -> TeamConfig:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / QUERIES_DIR).mkdir(exist_ok=True)
    manifest = root / MANIFEST_FILE
    cfg = TeamConfig(root=root, name=name)
    if not manifest.exists():
        manifest.write_text(
            json.dumps({"name": name, "members": [], "created_at": _now()}, indent=2),
            encoding="utf-8",
        )
    return cfg


def load_manifest(root: Path) -> dict[str, Any]:
    p = root / MANIFEST_FILE
    if not p.exists():
        return {"name": "team", "members": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"name": "team", "members": []}


def add_member(root: Path, member: str) -> dict[str, Any]:
    body = load_manifest(root)
    members = list(body.get("members") or [])
    if member not in members:
        members.append(member)
    body["members"] = members
    (root / MANIFEST_FILE).write_text(json.dumps(body, indent=2), encoding="utf-8")
    return body


# ---- shared annotations -------------------------------------------------


def push_annotation(
    team_root: Path,
    *,
    target: str,
    note: str,
    author: str,
    repo_sha: str = "",
) -> dict[str, Any]:
    """Append one annotation to the shared stream. Idempotent: a
    re-push of the same target+note+author is dropped.
    """
    p = team_root / ANNOTATIONS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(
        f"{target}::{note}::{author}".encode()
    ).hexdigest()[:16]
    # Idempotent check.
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                if row.get("fingerprint") == fingerprint:
                    return row
            except json.JSONDecodeError:
                continue
    row = {
        "fingerprint": fingerprint,
        "ts": _now(),
        "target": target,
        "note": note,
        "author": author,
        "repo_sha": repo_sha,
    }
    with p.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, separators=(",", ":")) + "\n")
    return row


def pull_annotations(team_root: Path) -> list[dict[str, Any]]:
    p = team_root / ANNOTATIONS_FILE
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def annotations_by_target(team_root: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in pull_annotations(team_root):
        out.setdefault(row.get("target", ""), []).append(row)
    return out


def detect_conflicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two annotations on the same target with different notes from
    different authors == conflict. Surfaces to the user, doesn't
    auto-merge.
    """
    by_target: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_target.setdefault(row.get("target", ""), []).append(row)
    conflicts: list[dict[str, Any]] = []
    for target, items in by_target.items():
        notes = {r.get("note") for r in items}
        authors = {r.get("author") for r in items}
        if len(notes) > 1 and len(authors) > 1:
            conflicts.append(
                {"target": target, "items": items, "n_distinct_notes": len(notes)}
            )
    return conflicts


def push_query(team_root: Path, name: str, gpl: str) -> Path:
    target = team_root / QUERIES_DIR / f"{name}.gpl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(gpl, encoding="utf-8")
    return target


def list_shared_queries(team_root: Path) -> list[dict[str, Any]]:
    base = team_root / QUERIES_DIR
    if not base.exists():
        return []
    rows: list[dict[str, Any]] = []
    for p in sorted(base.glob("*.gpl")):
        rows.append(
            {
                "name": p.stem,
                "path": str(p),
                "size": p.stat().st_size if p.exists() else 0,
                "preview": p.read_text(encoding="utf-8", errors="replace")[:120],
            }
        )
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ANNOTATIONS_FILE",
    "QUERIES_DIR",
    "TeamConfig",
    "add_member",
    "annotations_by_target",
    "detect_conflicts",
    "detect_team_root",
    "init_team",
    "list_shared_queries",
    "load_manifest",
    "pull_annotations",
    "push_annotation",
    "push_query",
]
