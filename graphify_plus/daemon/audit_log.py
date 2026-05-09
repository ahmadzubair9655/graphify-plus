"""Layer 20 — full audit trail with logical undo and branch namespacing.

Every change to the graph (annotation, correction, ingest, rebuild)
appends to ``.graphify_plus/audit.jsonl``. Replay the log + AST
extraction to recover any past state. Each record carries:

  ts, kind, agent, namespace, target, payload, parent_id

Logical undo: ``revert(record_id)`` writes a tombstone superseding the
original. The graph reads the latest non-tombstoned record per
``(namespace, target)`` pair.

Branch namespacing: each git branch gets its own annotation namespace.
``current_namespace`` reads ``git rev-parse --abbrev-ref HEAD``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("graphify_plus.daemon.audit_log")

AUDIT_FILE = "audit.jsonl"


@dataclass
class AuditRecord:
    id: str
    ts: str
    kind: str          # 'annotation' | 'correction' | 'ingest' | 'rebuild' | 'tombstone'
    agent: str = ""
    namespace: str = ""
    target: str = ""   # symbol_id or other entity id
    payload: dict[str, Any] = field(default_factory=dict)
    parent_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def audit_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / AUDIT_FILE


def current_namespace(repo: Path) -> str:
    """Returns the active git branch, or "main" if not a git repo."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
        return out or "main"
    except Exception:  # noqa: BLE001
        return "main"


def append(
    repo: Path,
    *,
    kind: str,
    target: str = "",
    agent: str = "",
    namespace: str | None = None,
    payload: dict[str, Any] | None = None,
    parent_id: str = "",
) -> AuditRecord:
    rec = AuditRecord(
        id=uuid.uuid4().hex[:12],
        ts=datetime.now(timezone.utc).isoformat(),
        kind=kind,
        agent=agent or "graphify-plus",
        namespace=namespace if namespace is not None else current_namespace(repo),
        target=target,
        payload=payload or {},
        parent_id=parent_id,
    )
    p = audit_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(rec.to_dict(), separators=(",", ":")) + "\n")
    return rec


def read_all(repo: Path) -> list[AuditRecord]:
    p = audit_path(repo)
    if not p.exists():
        return []
    out: list[AuditRecord] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                out.append(AuditRecord(**d))
            except (json.JSONDecodeError, TypeError):
                continue
    except OSError:
        return []
    return out


def revert(repo: Path, record_id: str, *, agent: str = "") -> AuditRecord:
    """Write a tombstone for ``record_id``. Returns the new tombstone."""
    return append(
        repo,
        kind="tombstone",
        target=record_id,
        agent=agent,
        payload={"superseded": record_id},
    )


def latest_state(
    repo: Path, *, namespace: str | None = None
) -> dict[tuple[str, str], AuditRecord]:
    """For each (namespace, target) pair, return the latest non-tombstoned
    record. Tombstones supersede the record they reference.
    """
    records = read_all(repo)
    tombstoned: set[str] = {
        r.target for r in records if r.kind == "tombstone"
    }
    cur: dict[tuple[str, str], AuditRecord] = {}
    for r in records:
        if r.kind == "tombstone":
            continue
        if r.id in tombstoned:
            continue
        if namespace is not None and r.namespace != namespace:
            continue
        key = (r.namespace, r.target)
        prev = cur.get(key)
        if prev is None or r.ts > prev.ts:
            cur[key] = r
    return cur


def by_agent(repo: Path) -> dict[str, int]:
    rows: dict[str, int] = defaultdict(int)
    for r in read_all(repo):
        rows[r.agent or "unknown"] += 1
    return dict(rows)


def rebuild_from_log(repo: Path) -> dict[str, Any]:
    """Layer 20.4 — replay the audit log to reconstruct the
    annotation/correction state. Returns a structured summary the
    caller can apply against the live store.

    The replay is deterministic: every record is read once in
    timestamp order, tombstones supersede their target. The output
    is the full list of annotations/corrections that should exist
    *now*, suitable for re-application after a cache loss.
    """
    records = read_all(repo)
    records.sort(key=lambda r: r.ts)
    tombstoned = {r.target for r in records if r.kind == "tombstone"}
    annotations: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    ingests: list[dict[str, Any]] = []
    rebuilds = 0
    for r in records:
        if r.kind == "tombstone":
            continue
        if r.id in tombstoned:
            continue
        if r.kind == "annotation":
            annotations.append(r.to_dict())
        elif r.kind == "correction":
            corrections.append(r.to_dict())
        elif r.kind == "ingest":
            ingests.append(r.to_dict())
        elif r.kind == "rebuild":
            rebuilds += 1
    return {
        "records_read": len(records),
        "tombstoned": len(tombstoned),
        "annotations_to_apply": annotations,
        "corrections_to_apply": corrections,
        "ingests_seen": ingests,
        "rebuilds_seen": rebuilds,
        "log_hash": hash_log(repo),
    }


def hash_log(repo: Path) -> str:
    """Compact deterministic hash of the entire audit log — useful for
    'two engineers on the same SHA see the same graph' (Layer 12.5)
    correctness assertions.
    """
    p = audit_path(repo)
    if not p.exists():
        return ""
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()[:16]


__all__ = [
    "AUDIT_FILE",
    "AuditRecord",
    "append",
    "audit_path",
    "by_agent",
    "current_namespace",
    "hash_log",
    "latest_state",
    "read_all",
    "rebuild_from_log",
    "revert",
]
