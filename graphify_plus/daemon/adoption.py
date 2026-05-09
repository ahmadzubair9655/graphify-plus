"""Adoption telemetry — answers the only post-merge question that matters.

The thesis of the entire master-plan rewrite is "Claude picks the graph
over grep when the question is structural." Without measurement, you
can't tell if it's true. This module ships the measurement surface
the review guide called for:

* **adoption_rate** — graph-tool calls / (graph-tool calls + grep calls)
  in a given time window.
* **hook_funnel** — how many grep calls saw a hook nudge, how many of
  those Claude accepted.
* **top_could_have_been_graph** — grep calls that the hook *would* have
  caught if it had fired (or did fire and Claude ignored).

Inputs:

* The existing daemon telemetry sink at
  ``.graphify_plus/telemetry.jsonl`` for graph-tool calls.
* A new ``.graphify_plus/grep-events.jsonl`` written by the pre-grep
  hook so we can count grep calls observed by the same Claude Code
  session.

The hook always writes its decision (whether or not it nudged) so we
get the full denominator. The graph telemetry already has the numerator.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("graphify_plus.daemon.adoption")

GREP_EVENTS_FILE = "grep-events.jsonl"


def grep_events_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / GREP_EVENTS_FILE


def record_grep_event(
    repo: Path,
    *,
    pattern: str,
    nudged: bool,
    bareword: bool,
    daemon_running: bool,
    fresh: bool,
    matched_node_count: int,
) -> None:
    """Append one grep observation. Called by the hook on every grep
    payload it sees, regardless of whether it nudged.
    """
    p = grep_events_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pattern": pattern[:80],
        "nudged": bool(nudged),
        "bareword": bool(bareword),
        "daemon_running": bool(daemon_running),
        "fresh": bool(fresh),
        "matched_node_count": int(matched_node_count),
    }
    try:
        with p.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError as exc:
        log.debug("grep event append failed: %s", exc)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return rows


def _within_window(ts: str, since: datetime) -> bool:
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return when >= since


@dataclass
class AdoptionReport:
    window_hours: int
    graph_calls: int
    grep_calls: int
    adoption_rate: float  # graph / (graph + grep)
    nudges_emitted: int
    nudges_could_have: int  # grep calls bareword + daemon FRESH + matches
    hook_potential_rate: float  # nudges_could_have / grep_calls
    top_missed: list[tuple[str, int]]  # (pattern, count) of bareword greps that DID match

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def adoption_report(repo: Path, *, window_hours: int = 24) -> AdoptionReport:
    from .telemetry import telemetry_path

    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    graph_rows = _read_jsonl(telemetry_path(repo))
    grep_rows = _read_jsonl(grep_events_path(repo))

    graph_in_window = sum(1 for r in graph_rows if _within_window(r.get("ts", ""), since))
    grep_in_window = [r for r in grep_rows if _within_window(r.get("ts", ""), since)]
    grep_count = len(grep_in_window)

    nudges = sum(1 for r in grep_in_window if r.get("nudged"))
    could_have = sum(
        1
        for r in grep_in_window
        if r.get("bareword")
        and r.get("daemon_running")
        and r.get("fresh")
        and r.get("matched_node_count", 0) > 0
    )
    total = graph_in_window + grep_count
    adoption = (graph_in_window / total) if total else 0.0
    potential = (could_have / grep_count) if grep_count else 0.0

    # Top bareword grep patterns that the daemon could have answered.
    counter: Counter[str] = Counter()
    for r in grep_in_window:
        if r.get("bareword") and r.get("matched_node_count", 0) > 0:
            counter[r.get("pattern", "")] += 1

    return AdoptionReport(
        window_hours=window_hours,
        graph_calls=graph_in_window,
        grep_calls=grep_count,
        adoption_rate=round(adoption, 3),
        nudges_emitted=nudges,
        nudges_could_have=could_have,
        hook_potential_rate=round(potential, 3),
        top_missed=counter.most_common(10),
    )


def render_report(report: AdoptionReport) -> str:
    lines: list[str] = []
    lines.append(f"# adoption — last {report.window_hours}h")
    lines.append("")
    lines.append(f"  graph calls : {report.graph_calls}")
    lines.append(f"  grep calls  : {report.grep_calls}")
    lines.append(
        f"  adoption rate: {report.adoption_rate:.1%}  (target: graph > 50% of structural queries)"
    )
    lines.append("")
    lines.append(
        f"  hook nudges : {report.nudges_emitted} of {report.nudges_could_have} "
        f"could-have-fired ({report.hook_potential_rate:.0%})"
    )
    lines.append("")
    if report.top_missed:
        lines.append("  top bareword greps that the graph could have answered:")
        for pat, n in report.top_missed:
            lines.append(f"    {n:>3}  {pat}")
    if report.adoption_rate >= 0.5:
        lines.append("")
        lines.append("  ✓ adoption target met (graph > 50%)")
    elif report.grep_calls + report.graph_calls == 0:
        lines.append("")
        lines.append("  · no events in window — install the pre-grep hook to start measuring")
    else:
        lines.append("")
        lines.append("  ✗ below target — investigate top_missed or push the routing skill harder")
    return "\n".join(lines)


__all__ = [
    "AdoptionReport",
    "GREP_EVENTS_FILE",
    "adoption_report",
    "grep_events_path",
    "record_grep_event",
    "render_report",
]
