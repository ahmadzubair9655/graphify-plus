"""12.4 — append-only JSONL log of confidence drift events.

When the same edge contributes to N audit failures, its confidence
should decay. We persist this as a human-readable JSONL file at
``<repo>/.graphify_plus/confidence_drift.log`` rather than a SQLite
table — append-only writes, no schema migration, easy to ``cat``.

Public surface
--------------

``record_drift(repo, edge_key, reason)``  — append one event.
``aggregate(repo)``                       — replay the log; return
    ``{edge_key: {"failures": int, "confidence": float}}``.
``apply_to_graph(G, repo)``               — mutate edge ``confidence``
    attrs in-place using ``aggregate()`` (floor 0.1).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

LOG_NAME = "confidence_drift.log"
DECREMENT = 0.1
FAILURE_THRESHOLD = 3  # 3+ failures before decrement applies
FLOOR = 0.1


def _log_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / LOG_NAME


def edge_key(src: str, dst: str, kind: str) -> str:
    return f"{src}|{dst}|{kind}"


def record_drift(repo: Path, key: str, reason: str) -> None:
    p = _log_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": int(time.time()), "edge": key, "reason": reason}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def aggregate(repo: Path) -> dict[str, dict[str, Any]]:
    p = _log_path(repo)
    failures: dict[str, int] = {}
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            k = entry.get("edge")
            if isinstance(k, str):
                failures[k] = failures.get(k, 0) + 1
    out: dict[str, dict[str, Any]] = {}
    for k, n in failures.items():
        if n >= FAILURE_THRESHOLD:
            decay = (n - FAILURE_THRESHOLD + 1) * DECREMENT
            out[k] = {"failures": n, "decay": decay}
    return out


def apply_to_graph(G, repo: Path) -> int:
    """Apply aggregated drift to live graph edge attrs. Returns the
    number of edges adjusted. Floor is ``FLOOR`` (0.1)."""
    drift = aggregate(repo)
    if not drift:
        return 0
    adjusted = 0
    for u, v, _k, data in G.edges(keys=True, data=True):
        kind = data.get("kind") or ""
        k = edge_key(str(u), str(v), kind)
        if k in drift:
            current = float(data.get("confidence", 0.2))
            new = max(FLOOR, current - drift[k]["decay"])
            if new != current:
                data["confidence"] = new
                adjusted += 1
    return adjusted


DRIFT_PROBES: tuple[tuple[str, str], ...] = (
    ("unresolved_imports", "unresolved_imports_grade"),
    ("phantom_symbols", "phantom_grade"),
    ("low_confidence_ratio", "low_confidence_grade"),
)


def record_from_audit(audit: dict, repo: Path) -> int:
    """Per the v4.4.0 wiring policy: when any of the three drift-signal
    probes returns grade D or F, append a drift event for every
    contributing edge. Returns the number of events appended.
    """
    events = 0
    for probe_key, grade_field in DRIFT_PROBES:
        result = audit.get(probe_key, {})
        if result.get("skipped"):
            continue
        grade = result.get(grade_field)
        if grade not in {"D", "F"}:
            continue
        for src, dst, kind in result.get("contributing_edges", []):
            record_drift(repo, edge_key(src, dst, kind), reason=f"{probe_key}:{grade}")
            events += 1
    return events


__all__ = [
    "DECREMENT",
    "DRIFT_PROBES",
    "FAILURE_THRESHOLD",
    "FLOOR",
    "LOG_NAME",
    "aggregate",
    "apply_to_graph",
    "edge_key",
    "record_drift",
    "record_from_audit",
]
