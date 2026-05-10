"""Layer 14.3 — ground-truth benchmark harness.

A scaffold that runs graphify-plus against a benchmark corpus with
hand-labeled "gold" expectations and reports precision/recall per
question class.

The corpus format is intentionally simple — JSON files declaring
expected values for each question class. A built-in tiny fixture
ships in ``tests/fixtures/benchmark_tiny/``; the full curated corpus
of 5–10 mid-sized OSS repos is a separate artefact (per the master
plan, "ships separately").

Each entry:

    {
      "name": "auth-fixture",
      "repo_path": "tests/fixtures/auth",
      "checks": [
        {"op": "find_by_name", "args": {"label": "AuthService"}, "expect_at_least": 1},
        {"op": "who_calls", "args": {"node": "AuthService.login"}, "expect_includes": ["helper"]}
      ]
    }
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .indexes import InMemoryGraph

log = logging.getLogger("graphify_plus.daemon.benchmark")


@dataclass
class BenchmarkCheck:
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    expect_at_least: int = 0
    expect_includes: list[str] = field(default_factory=list)
    expect_max_pct: float | None = None


@dataclass
class BenchmarkEntry:
    name: str
    repo_path: str
    checks: list[BenchmarkCheck] = field(default_factory=list)


@dataclass
class CheckResult:
    op: str
    passed: bool
    actual_count: int
    notes: str = ""


@dataclass
class BenchmarkReport:
    name: str
    total: int = 0
    passed: int = 0
    results: list[CheckResult] = field(default_factory=list)

    def precision(self) -> float:
        return (self.passed / self.total) if self.total else 0.0


def load_corpus(path: Path) -> list[BenchmarkEntry]:
    """Read a corpus JSON file. Each entry produces one ``BenchmarkEntry``."""
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, list):
        raise ValueError(f"corpus must be a list of entries: {path}")
    rows: list[BenchmarkEntry] = []
    for raw in body:
        rows.append(
            BenchmarkEntry(
                name=raw.get("name", "?"),
                repo_path=raw.get("repo_path", ""),
                checks=[
                    BenchmarkCheck(
                        op=c.get("op", "?"),
                        args=c.get("args") or {},
                        expect_at_least=int(c.get("expect_at_least", 0)),
                        expect_includes=list(c.get("expect_includes") or []),
                        expect_max_pct=c.get("expect_max_pct"),
                    )
                    for c in raw.get("checks") or []
                ],
            )
        )
    return rows


def run_check(graph: InMemoryGraph, check: BenchmarkCheck) -> CheckResult:
    from .handlers import HANDLERS

    handler = HANDLERS.get(check.op)
    if handler is None:
        return CheckResult(op=check.op, passed=False, actual_count=0, notes="unknown op")
    try:
        out = handler(graph, dict(check.args))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(op=check.op, passed=False, actual_count=0, notes=str(exc))
    if "error" in out:
        return CheckResult(
            op=check.op,
            passed=check.expect_at_least == 0,
            actual_count=0,
            notes=out["error"].get("message", ""),
        )
    rows = out.get("results") or []
    actual = len(rows)
    passed = True
    notes_parts: list[str] = []
    if actual < check.expect_at_least:
        passed = False
        notes_parts.append(f"expected >={check.expect_at_least} got {actual}")
    if check.expect_includes:
        labels = " ".join(str(r.get("label") or "") for r in rows)
        for needle in check.expect_includes:
            if needle not in labels:
                passed = False
                notes_parts.append(f"missing {needle!r}")
    if check.expect_max_pct is not None and actual:
        first = rows[0]
        if (first.get("coverage_pct") or 100.0) > check.expect_max_pct:
            passed = False
            notes_parts.append(
                f"coverage_pct {first.get('coverage_pct')} > max {check.expect_max_pct}"
            )
    return CheckResult(
        op=check.op,
        passed=passed,
        actual_count=actual,
        notes="; ".join(notes_parts),
    )


def run_entry(entry: BenchmarkEntry, graph: InMemoryGraph) -> BenchmarkReport:
    report = BenchmarkReport(name=entry.name)
    for check in entry.checks:
        res = run_check(graph, check)
        report.total += 1
        if res.passed:
            report.passed += 1
        report.results.append(res)
    return report


def render_report(reports: list[BenchmarkReport]) -> str:
    lines: list[str] = ["# graphify-plus benchmark report"]
    lines.append("")
    total = sum(r.total for r in reports)
    passed = sum(r.passed for r in reports)
    lines.append(
        f"Overall: **{passed}/{total} passed** ({passed / total * 100:.1f}% precision)"
        if total
        else "no checks ran"
    )
    lines.append("")
    for r in reports:
        lines.append(f"## {r.name}")
        lines.append(f"  {r.passed}/{r.total} passed ({r.precision() * 100:.1f}%)")
        for res in r.results:
            mark = "✓" if res.passed else "✗"
            lines.append(f"    {mark} {res.op:<20}  count={res.actual_count}  {res.notes}")
        lines.append("")
    return "\n".join(lines)


# ---- built-in tiny fixture corpus --------------------------------------


BUILTIN_TINY_CORPUS_JSON = [
    {
        "name": "auth-fixture",
        "repo_path": "<fixture-repo>",
        "checks": [
            {"op": "find_by_name", "args": {"label": "auth"}, "expect_at_least": 1},
            {"op": "who_calls", "args": {"node": "AuthService.login"}, "expect_at_least": 1},
            {
                "op": "what_depends_on",
                "args": {"node": "AuthService.validate"},
                "expect_at_least": 1,
            },
        ],
    }
]


def builtin_tiny_corpus() -> list[BenchmarkEntry]:
    rows: list[BenchmarkEntry] = []
    for raw in BUILTIN_TINY_CORPUS_JSON:
        entry: dict[str, Any] = dict(raw)  # type: ignore[arg-type]
        rows.append(
            BenchmarkEntry(
                name=str(entry["name"]),
                repo_path=str(entry["repo_path"]),
                checks=[
                    BenchmarkCheck(
                        op=str(c["op"]),
                        args=dict(c.get("args", {})),
                        expect_at_least=int(c.get("expect_at_least", 0)),
                        expect_includes=[str(x) for x in c.get("expect_includes", [])],
                    )
                    for c in entry["checks"]
                ],
            )
        )
    return rows


__all__ = [
    "BUILTIN_TINY_CORPUS_JSON",
    "BenchmarkCheck",
    "BenchmarkEntry",
    "BenchmarkReport",
    "CheckResult",
    "builtin_tiny_corpus",
    "load_corpus",
    "render_report",
    "run_check",
    "run_entry",
]
