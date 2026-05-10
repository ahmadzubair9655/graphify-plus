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
BASELINE_FILE = "adoption-baseline.json"
NUDGE_ACCEPT_WINDOW_S = 30.0  # window for "graph call follows nudge"


def grep_events_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / GREP_EVENTS_FILE


def baseline_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / BASELINE_FILE


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
    # Signal-to-noise of the hook itself. A high adoption_rate paired with a
    # low nudge_accept_rate means the win is coming from somewhere other
    # than the nudge — Claude reading SKILL.md, the routing skill, or just
    # learning the tool surface. Both numbers matter.
    nudge_accept_rate: float = 0.0
    nudges_followed_by_graph_call: int = 0
    # Comparison against a stored baseline. When no baseline is set,
    # baseline_* fields are None and delta_* fields are 0.
    baseline_adoption_rate: float | None = None
    delta_adoption_rate: float = 0.0
    # Latency distribution of *real* graph queries in the window. The
    # perfcheck table is a proxy; this is what users actually feel.
    # One outlier query above 100ms is a louder signal than a thousand
    # fast ones, so we surface P50/P95/P99 + slowest_op.
    query_latency_p50_ms: float = 0.0
    query_latency_p95_ms: float = 0.0
    query_latency_p99_ms: float = 0.0
    slowest_op: str = ""
    slowest_op_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def adoption_report(repo: Path, *, window_hours: int = 24) -> AdoptionReport:
    from .telemetry import telemetry_path

    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    graph_rows = _read_jsonl(telemetry_path(repo))
    grep_rows = _read_jsonl(grep_events_path(repo))

    graph_in_window_rows = [r for r in graph_rows if _within_window(r.get("ts", ""), since)]
    grep_in_window = [r for r in grep_rows if _within_window(r.get("ts", ""), since)]
    graph_in_window = len(graph_in_window_rows)
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

    # Signal/noise: how many nudges were followed by a graph call within
    # NUDGE_ACCEPT_WINDOW_S? The hook noise floor (RISKS.md R6) becomes
    # measurable instead of just monitored.
    nudge_followed = _count_nudge_accepts(grep_in_window, graph_in_window_rows)
    nudge_rate = (nudge_followed / nudges) if nudges else 0.0

    # Top bareword grep patterns that the daemon could have answered.
    counter: Counter[str] = Counter()
    for r in grep_in_window:
        if r.get("bareword") and r.get("matched_node_count", 0) > 0:
            counter[r.get("pattern", "")] += 1

    # Baseline comparison.
    baseline = load_baseline(repo)
    baseline_adoption = baseline.get("adoption_rate") if baseline else None
    delta = (adoption - baseline_adoption) if baseline_adoption is not None else 0.0

    # Latency distribution of real queries (Production P99, not synthetic
    # perfcheck). One slow real query is a louder signal than 1k fast
    # ones — surface it.
    p50 = p95 = p99 = 0.0
    slow_op = ""
    slow_ms = 0.0
    if graph_in_window_rows:
        latencies = sorted(float(r.get("elapsed_ms", 0.0) or 0.0) for r in graph_in_window_rows)
        n = len(latencies)
        p50 = latencies[n // 2]
        p95 = latencies[min(int(n * 0.95), n - 1)]
        p99 = latencies[min(int(n * 0.99), n - 1)]
        worst = max(graph_in_window_rows, key=lambda r: float(r.get("elapsed_ms", 0.0) or 0.0))
        slow_op = str(worst.get("op", ""))
        slow_ms = float(worst.get("elapsed_ms", 0.0) or 0.0)

    return AdoptionReport(
        window_hours=window_hours,
        graph_calls=graph_in_window,
        grep_calls=grep_count,
        adoption_rate=round(adoption, 3),
        nudges_emitted=nudges,
        nudges_could_have=could_have,
        hook_potential_rate=round(potential, 3),
        top_missed=counter.most_common(10),
        nudge_accept_rate=round(nudge_rate, 3),
        nudges_followed_by_graph_call=nudge_followed,
        baseline_adoption_rate=baseline_adoption,
        delta_adoption_rate=round(delta, 3),
        query_latency_p50_ms=round(p50, 3),
        query_latency_p95_ms=round(p95, 3),
        query_latency_p99_ms=round(p99, 3),
        slowest_op=slow_op,
        slowest_op_ms=round(slow_ms, 3),
    )


def _count_nudge_accepts(grep_rows: list[dict[str, Any]], graph_rows: list[dict[str, Any]]) -> int:
    """For each nudge fired, did a graph tool call land within
    NUDGE_ACCEPT_WINDOW_S? Counts each nudge at most once.
    """
    nudge_times: list[float] = []
    for r in grep_rows:
        if not r.get("nudged"):
            continue
        try:
            t = datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        nudge_times.append(t)
    if not nudge_times:
        return 0
    graph_times: list[float] = []
    for r in graph_rows:
        if not r.get("ok", True):
            continue
        try:
            t = datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        graph_times.append(t)
    graph_times.sort()
    accepted = 0
    for nt in nudge_times:
        # Earliest graph call within the window after the nudge.
        for gt in graph_times:
            if gt < nt:
                continue
            if gt - nt <= NUDGE_ACCEPT_WINDOW_S:
                accepted += 1
            break
    return accepted


# ---- baseline support ---------------------------------------------------


def write_baseline(repo: Path, report: AdoptionReport, *, label: str = "v6.0") -> Path:
    """Snapshot the current adoption state as the comparison baseline.

    Subsequent ``adoption_report`` runs will compute ``delta_adoption_rate``
    against this snapshot. Re-running ``write_baseline`` overwrites the
    previous baseline.
    """
    p = baseline_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "label": label,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": report.window_hours,
        "adoption_rate": report.adoption_rate,
        "graph_calls": report.graph_calls,
        "grep_calls": report.grep_calls,
        "nudge_accept_rate": report.nudge_accept_rate,
    }
    p.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return p


def load_baseline(repo: Path) -> dict[str, Any] | None:
    p = baseline_path(repo)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
    lines.append(
        f"  nudge accept: {report.nudges_followed_by_graph_call}/{report.nudges_emitted} "
        f"= {report.nudge_accept_rate:.0%}  "
        f"(graph call within {NUDGE_ACCEPT_WINDOW_S:.0f}s of nudge)"
    )
    if report.graph_calls > 0:
        lines.append(
            f"  query latency: p50={report.query_latency_p50_ms}ms  "
            f"p95={report.query_latency_p95_ms}ms  "
            f"p99={report.query_latency_p99_ms}ms"
        )
        if report.slowest_op_ms >= 100.0:
            lines.append(
                f"  ⚠ slowest production query: {report.slowest_op} "
                f"= {report.slowest_op_ms}ms (above 100ms — "
                "investigate, not a perfcheck artifact)"
            )
    if report.baseline_adoption_rate is not None:
        sign = "+" if report.delta_adoption_rate >= 0 else ""
        lines.append(
            f"  vs baseline : {report.baseline_adoption_rate:.1%} → "
            f"{report.adoption_rate:.1%}  "
            f"({sign}{report.delta_adoption_rate:.1%})"
        )
    else:
        lines.append(
            "  vs baseline : no baseline stored — run `gp daemon adoption-baseline set` "
            "to capture the current state as the comparison anchor"
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
    "BASELINE_FILE",
    "GREP_EVENTS_FILE",
    "NUDGE_ACCEPT_WINDOW_S",
    "adoption_report",
    "baseline_path",
    "grep_events_path",
    "load_baseline",
    "record_grep_event",
    "render_report",
    "write_baseline",
]
