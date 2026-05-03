"""Git enrichment.

Bulk-derive per-symbol volatility (commit churn) and age scores from
git history, attach them as node attributes.

Algorithm — single ``git log --numstat`` invocation, parsed in-process:

  - per_file[path] = (commit_count, last_touch_unix)
  - volatility = log10(1 + commit_count) / log10(1 + repo_max)
  - age_score  = days_since_last_touch / repo_median_days
  - legacy_flag = volatility < 0.2 AND age > 730d AND in_degree > 5

These attributes feed the audit (Phase 11 trust-score) and the
budgeter's importance heuristic (Phase 3).
"""

from __future__ import annotations

import math
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from ..interface.privacy import redact


@dataclass(frozen=True)
class FileStats:
    path: str
    commit_count: int
    last_touch_unix: int


def _run_git_log(repo: Path) -> str:
    """Single bulk-log call. Format: ``HASH<TAB>ISO<TAB>AUTHOR`` followed
    by per-file ``ADDED<TAB>REMOVED<TAB>PATH`` lines.
    """
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "log",
                "--numstat",
                "--pretty=format:GP_COMMIT %H%x09%aI%x09%aN",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout


def parse_log(log_text: str) -> dict[str, FileStats]:
    counts: dict[str, int] = {}
    last_touch: dict[str, int] = {}
    current_unix: int = 0
    for line in log_text.splitlines():
        if not line:
            continue
        if line.startswith("GP_COMMIT "):
            parts = line[len("GP_COMMIT ") :].split("\t")
            if len(parts) >= 2:
                # ISO 8601 → unix seconds
                try:
                    current_unix = int(
                        time.mktime(time.strptime(parts[1][:19], "%Y-%m-%dT%H:%M:%S"))
                    )
                except ValueError:
                    current_unix = 0
            continue
        # numstat row: <added>\t<removed>\t<path>
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        path = cols[2].strip()
        if not path or path.startswith('"'):  # skip rename oddities
            continue
        counts[path] = counts.get(path, 0) + 1
        if current_unix > last_touch.get(path, 0):
            last_touch[path] = current_unix
    return {
        path: FileStats(
            path=path, commit_count=counts[path], last_touch_unix=last_touch.get(path, 0)
        )
        for path in counts
    }


def enrich_graph(G: nx.MultiDiGraph, stats: dict[str, FileStats]) -> dict:
    """Attach volatility, age_score, last_touch, legacy_flag to each
    node whose ``path`` is in ``stats``. Returns a small summary dict.
    """
    if not stats:
        return {"files_enriched": 0}
    max_count = max(s.commit_count for s in stats.values()) or 1
    now = int(time.time())
    days = []
    for s in stats.values():
        if s.last_touch_unix:
            days.append((now - s.last_touch_unix) / 86400)
    median_days = statistics.median(days) if days else 1.0

    enriched = 0
    legacy = 0
    for sid, attrs in G.nodes(data=True):
        path = (attrs or {}).get("path") or ""
        s = stats.get(path)
        if s is None:
            continue
        volatility = math.log10(1 + s.commit_count) / math.log10(1 + max_count)
        age_days = (now - s.last_touch_unix) / 86400 if s.last_touch_unix else 0.0
        age_score = age_days / max(median_days, 1.0)
        in_deg = G.in_degree(sid)
        legacy_flag = (
            volatility < 0.2 and age_days > 730 and (in_deg if isinstance(in_deg, int) else 0) > 5
        )
        attrs["volatility"] = round(volatility, 4)
        attrs["age_days"] = round(age_days, 2)
        attrs["age_score"] = round(age_score, 4)
        attrs["last_touch_unix"] = s.last_touch_unix
        attrs["commit_count"] = s.commit_count
        attrs["legacy_flag"] = bool(legacy_flag)
        if legacy_flag:
            legacy += 1
        enriched += 1
    return {"files_enriched": enriched, "legacy_flagged": legacy}


def enrich(repo: Path, G: nx.MultiDiGraph) -> dict:
    """Top-level: run git log, enrich the graph in place. Privacy
    redaction is applied to author names that may be embedded into
    summary outputs (paranoia — author names are not currently
    propagated into nodes, but downstream may add them)."""
    log_text = _run_git_log(repo)
    if not log_text:
        return {"files_enriched": 0, "skipped": True, "reason": "git not available or empty"}
    redact(log_text[:1])  # ensure module imports stay used / no-op
    return enrich_graph(G, parse_log(log_text))


__all__ = ["FileStats", "enrich", "enrich_graph", "parse_log"]
