"""Local-only telemetry sink for the daemon — Sprint 5 of the master plan.

Every successful daemon dispatch appends a single JSON line to
``<repo>/.graphify_plus/telemetry.jsonl`` recording:

    {
        "ts": "2026-05-09T17:31:35.720116+00:00",
        "op": "who_calls",
        "elapsed_ms": 0.05,
        "tokens": 38,
        "n_results": 1,
        "trust": "FRESH",
        "ok": true
    }

This is the *only* metric the master plan calls "the only one that
matters: adoption rate per query type." It stays on the user's machine
by default. The existing ``interface.telemetry`` HTTP exporter is opt-in
and continues to handle outbound emissions when configured.

``aggregate(repo)`` reads the JSONL, groups by op, and returns counts,
P50/P95 latency, FRESH rate, and total tokens. ``gp daemon stats``
renders that.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("graphify_plus.daemon.telemetry")

TELEMETRY_FILE = "telemetry.jsonl"
MAX_LINES = 10_000  # roll forward when the file grows past this; cheap audit history


def telemetry_path(repo_root: Path) -> Path:
    return repo_root / ".graphify_plus" / TELEMETRY_FILE


def append_event(
    repo_root: Path,
    op: str,
    *,
    elapsed_ms: float,
    tokens: int,
    n_results: int,
    trust: str,
    ok: bool,
    error_code: str | None = None,
) -> None:
    """Append one event. Never raises — telemetry must not break the
    daemon. Errors are logged at debug level so they're visible under
    ``GP_DEBUG=1`` but invisible otherwise.
    """
    try:
        path = telemetry_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        event: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op,
            "elapsed_ms": round(float(elapsed_ms), 3),
            "tokens": int(tokens),
            "n_results": int(n_results),
            "trust": trust,
            "ok": bool(ok),
        }
        if error_code:
            event["error_code"] = error_code
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, separators=(",", ":")) + "\n")
        # Cheap log rotation. Reads the whole file periodically — fine at
        # ~10k lines, which is days-of-use scale.
        if path.stat().st_size > 2 * 1024 * 1024:  # >2MB
            _truncate(path)
    except Exception as exc:  # noqa: BLE001
        log.debug("telemetry append failed: %s", exc)


def _truncate(path: Path) -> None:
    """Keep the most recent MAX_LINES rows."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        keep = lines[-MAX_LINES:]
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError:
        pass


def aggregate(repo_root: Path) -> dict[str, Any]:
    """Compute summary stats over the local telemetry log.

    Returned shape (suitable for JSON output)::

        {
            "total_calls": 412,
            "ok_rate": 0.99,
            "fresh_rate": 0.87,
            "by_op": {
                "who_calls": {
                    "calls": 87,
                    "p50_ms": 0.32,
                    "p95_ms": 1.8,
                    "tokens_total": 12_400,
                    "fresh_rate": 1.0,
                    "ok_rate": 1.0,
                    "avg_results": 4.2
                },
                ...
            }
        }
    """
    path = telemetry_path(repo_root)
    if not path.exists():
        return {"total_calls": 0, "ok_rate": 0.0, "fresh_rate": 0.0, "by_op": {}}

    by_op: dict[str, list[dict[str, Any]]] = defaultdict(list)
    total = 0
    ok_count = 0
    fresh_count = 0
    try:
        with path.open(encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                if ev.get("ok"):
                    ok_count += 1
                if ev.get("trust") in ("FRESH", "LIVE_AHEAD"):
                    fresh_count += 1
                by_op[ev.get("op") or "?"].append(ev)
    except OSError as exc:
        log.debug("telemetry read failed: %s", exc)
        return {"total_calls": 0, "ok_rate": 0.0, "fresh_rate": 0.0, "by_op": {}}

    summary: dict[str, Any] = {
        "total_calls": total,
        "ok_rate": round(ok_count / total, 4) if total else 0.0,
        "fresh_rate": round(fresh_count / total, 4) if total else 0.0,
        "by_op": {},
    }
    for op, events in by_op.items():
        latencies = [float(e.get("elapsed_ms", 0.0)) for e in events]
        latencies.sort()
        n = len(events)
        summary["by_op"][op] = {
            "calls": n,
            "p50_ms": _quantile(latencies, 0.50),
            "p95_ms": _quantile(latencies, 0.95),
            "tokens_total": sum(int(e.get("tokens", 0)) for e in events),
            "fresh_rate": round(
                sum(1 for e in events if e.get("trust") in ("FRESH", "LIVE_AHEAD")) / n, 4
            ),
            "ok_rate": round(sum(1 for e in events if e.get("ok")) / n, 4),
            "avg_results": round(sum(int(e.get("n_results", 0)) for e in events) / n, 2),
        }
    # Top errors, if any — useful for debugging adoption blockers.
    errors = Counter(
        e.get("error_code") for events in by_op.values() for e in events if not e.get("ok")
    )
    errors.pop(None, None)
    if errors:
        summary["top_errors"] = errors.most_common(5)
    return summary


def _quantile(sorted_xs: list[float], q: float) -> float:
    if not sorted_xs:
        return 0.0
    if len(sorted_xs) == 1:
        return round(sorted_xs[0], 3)
    # Standard linear interpolation; sufficient for single-digit-precision reporting.
    return round(statistics.quantiles(sorted_xs, n=100)[int(q * 100) - 1], 3)


__all__ = ["TELEMETRY_FILE", "aggregate", "append_event", "telemetry_path"]
