"""Layer 19 — anomaly detection, trend, weekly digest.

The watcher daemon, between updates, runs cheap anomaly checks against
the current and previous snapshots. Anomalies emit events; users opt
into where they go (terminal, notification, webhook).

Health time series — every rebuild snapshots a few summary numbers to
``.graphify_plus/health-history.jsonl``. ``gp daemon trend`` plots the
recent past as ASCII so a single command answers "is our codebase
getting healthier or sicker?".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .indexes import InMemoryGraph

log = logging.getLogger("graphify_plus.daemon.anomaly")

HEALTH_FILE = "health-history.jsonl"


@dataclass
class HealthSnapshot:
    ts: str
    n_symbols: int
    n_files: int
    n_edges: int
    god_node_count: int  # |pagerank_top|
    fresh_token: str
    coverage_pct: float | None = None
    rule_violations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def health_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / HEALTH_FILE


def take_snapshot(graph: InMemoryGraph, *, rule_violations: int = 0) -> HealthSnapshot:
    cov = None
    if graph.coverage:
        lc = sum(int(c.get("lines_covered", 0)) for c in graph.coverage.values())
        lt = sum(int(c.get("lines_total", 0)) for c in graph.coverage.values())
        cov = round(lc / lt * 100.0, 1) if lt else None
    return HealthSnapshot(
        ts=datetime.now(timezone.utc).isoformat(),
        n_symbols=len(graph.by_id),
        n_files=len(graph.by_path),
        n_edges=sum(len(v) for v in graph.out_neighbours.values()),
        god_node_count=len(graph.pagerank_top),
        fresh_token=graph.freshness_token,
        coverage_pct=cov,
        rule_violations=int(rule_violations),
    )


def append_snapshot(repo: Path, snap: HealthSnapshot) -> None:
    p = health_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(snap.to_dict(), separators=(",", ":")) + "\n")


def history(repo: Path, *, limit: int = 90) -> list[dict[str, Any]]:
    p = health_path(repo)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out[-limit:]


# ---- anomaly detection --------------------------------------------------


@dataclass
class Anomaly:
    code: str
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


def detect(prev: HealthSnapshot, cur: HealthSnapshot) -> list[Anomaly]:
    out: list[Anomaly] = []
    if prev.god_node_count and cur.god_node_count <= prev.god_node_count * 0.7:
        out.append(
            Anomaly(
                code="GOD_NODE_DROP",
                severity="warning",
                message=(
                    f"god-node count fell from {prev.god_node_count} to "
                    f"{cur.god_node_count} (>=30% drop)"
                ),
            )
        )
    if cur.coverage_pct is not None and prev.coverage_pct is not None:
        if cur.coverage_pct < prev.coverage_pct - 5.0:
            out.append(
                Anomaly(
                    code="COVERAGE_DROP",
                    severity="warning",
                    message=(
                        f"coverage fell from {prev.coverage_pct}% to "
                        f"{cur.coverage_pct}% (>5pt drop)"
                    ),
                )
            )
    if cur.rule_violations > prev.rule_violations:
        out.append(
            Anomaly(
                code="NEW_RULE_VIOLATIONS",
                severity="error",
                message=(
                    f"{cur.rule_violations - prev.rule_violations} new rule "
                    "violation(s) introduced since last snapshot"
                ),
            )
        )
    if cur.n_symbols == 0 and prev.n_symbols > 0:
        out.append(
            Anomaly(
                code="EMPTY_GRAPH",
                severity="error",
                message="graph rebuilt to zero symbols — likely an extractor failure",
            )
        )
    return out


# ---- trend rendering ---------------------------------------------------


def trend_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"snapshots": 0}
    first = rows[0]
    last = rows[-1]
    return {
        "snapshots": len(rows),
        "from": first.get("ts"),
        "to": last.get("ts"),
        "delta": {
            "n_symbols": last.get("n_symbols", 0) - first.get("n_symbols", 0),
            "n_edges": last.get("n_edges", 0) - first.get("n_edges", 0),
            "coverage_pct": (
                None
                if first.get("coverage_pct") is None or last.get("coverage_pct") is None
                else round(last["coverage_pct"] - first["coverage_pct"], 1)
            ),
            "rule_violations": last.get("rule_violations", 0) - first.get("rule_violations", 0),
        },
    }


def ascii_sparkline(values: list[float], *, width: int = 40) -> str:
    if not values:
        return ""
    if len(values) > width:
        # downsample by averaging.
        step = len(values) / width
        sampled: list[float] = []
        for i in range(width):
            start = int(i * step)
            end = int((i + 1) * step) or start + 1
            chunk = values[start:end] or [values[-1]]
            sampled.append(sum(chunk) / len(chunk))
        values = sampled
    chars = "▁▂▃▄▅▆▇█"
    lo = min(values)
    hi = max(values)
    span = hi - lo or 1.0
    return "".join(
        chars[min(int((v - lo) / span * (len(chars) - 1)), len(chars) - 1)] for v in values
    )


def render_trend(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no health snapshots yet — run `gp init` and a few queries first"
    summary = trend_summary(rows)
    out: list[str] = []
    out.append(f"# health trend ({summary['snapshots']} snapshots)")
    out.append(f"_{summary['from']} → {summary['to']}_")
    out.append("")
    syms = [float(r.get("n_symbols", 0)) for r in rows]
    edges = [float(r.get("n_edges", 0)) for r in rows]
    cov = [float(r.get("coverage_pct") or 0.0) for r in rows]
    rv = [float(r.get("rule_violations", 0)) for r in rows]
    out.append(f"  symbols    {ascii_sparkline(syms)}  Δ {summary['delta']['n_symbols']:+d}")
    out.append(f"  edges      {ascii_sparkline(edges)}  Δ {summary['delta']['n_edges']:+d}")
    if any(cov):
        out.append(f"  coverage   {ascii_sparkline(cov)}  Δ {summary['delta']['coverage_pct']}")
    out.append(f"  violations {ascii_sparkline(rv)}  Δ {summary['delta']['rule_violations']:+d}")
    return "\n".join(out)


# ---- weekly digest ------------------------------------------------------


def render_weekly(repo: Path, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no health history yet"
    last7 = rows[-7:]
    out: list[str] = []
    out.append(f"# weekly digest — {repo.name}")
    out.append("")
    summary = trend_summary(last7)
    out.append(f"snapshots: {summary['snapshots']}")
    delta = summary["delta"]
    out.append(f"symbols:    Δ {delta['n_symbols']:+d}")
    out.append(f"edges:      Δ {delta['n_edges']:+d}")
    if delta.get("coverage_pct") is not None:
        out.append(f"coverage:   Δ {delta['coverage_pct']}pt")
    out.append(f"violations: Δ {delta['rule_violations']:+d}")
    out.append("")
    out.append(render_trend(last7))
    return "\n".join(out)


__all__ = [
    "Anomaly",
    "HealthSnapshot",
    "append_snapshot",
    "ascii_sparkline",
    "detect",
    "health_path",
    "history",
    "render_trend",
    "render_weekly",
    "take_snapshot",
    "trend_summary",
]
