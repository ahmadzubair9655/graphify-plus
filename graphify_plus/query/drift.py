"""Drift enforcement: continuously evaluate the rules engine against the
live graph state, optionally driven by the Phase 2 watcher.

Differences vs guardrails:
  - guardrails simulate a *proposed* edit before it lands;
  - drift evaluates whatever's already on disk after every save.

Differences vs shadow:
  - shadow takes a manual overlay and grades it;
  - drift uses an empty overlay and reports current real violations.

When run as a daemon, drift writes ``.graphify_plus/drift_report.md`` in
the **target repo** (never inside graphify-plus itself).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..core.symbol_graph import build as build_graph
from ..runtime.overlay import empty
from ..runtime.rules import RuleSet, Violation, evaluate, grade
from ..runtime.store import Store, cache_path
from ..runtime.watcher import FileUpdate, Watcher

log = logging.getLogger("graphify_plus.drift")


@dataclass(frozen=True)
class DriftReport:
    grade: str
    violations: list[Violation]


def _load_graph(store: Store):
    symbols = store.all_symbols()
    edges = store.all_edges()
    return build_graph(symbols, edges)


def check(repo: Path, ruleset: RuleSet) -> DriftReport:
    """One-shot drift check against the current cache."""
    store = Store(cache_path(repo))
    try:
        G = _load_graph(store)
    finally:
        store.close()
    overlay = empty(G)
    vios = evaluate(overlay, ruleset)
    return DriftReport(grade=grade(vios), violations=vios)


def render_report(report: DriftReport) -> str:
    lines = [
        "# Drift Report",
        "",
        f"_Grade:_ **{report.grade}**",
        "",
    ]
    if not report.violations:
        lines.append("No active rule violations.")
        return "\n".join(lines) + "\n"
    lines.append("## Violations")
    lines.append("")
    lines.append("| rule | severity | src | dst | kind | message |")
    lines.append("|------|----------|-----|-----|------|---------|")
    for v in report.violations:
        lines.append(
            f"| `{v.rule_id}` | {v.severity} | `{v.src or ''}` | `{v.dst or ''}` "
            f"| `{v.kind or ''}` | {v.message} |"
        )
    return "\n".join(lines) + "\n"


class DriftDaemon:
    """Subscribes to the watcher; re-evaluates rules on every save and
    writes ``<target_repo>/.graphify_plus/drift_report.md``."""

    def __init__(self, repo: Path, ruleset: RuleSet):
        self.repo = repo.resolve()
        self.ruleset = ruleset
        self._watcher: Watcher | None = None
        self._lock = threading.Lock()

    def start(self, on_event: Callable[[DriftReport], None] | None = None) -> None:
        w = Watcher(self.repo)

        def _on_update(_u: FileUpdate) -> None:
            with self._lock:
                report = check(self.repo, self.ruleset)
                out = self.repo / ".graphify_plus" / "drift_report.md"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(render_report(report))
                if on_event is not None:
                    on_event(report)

        w.subscribe(_on_update)
        w.start()
        self._watcher = w
        # Initial evaluation, no edit needed.
        report = check(self.repo, self.ruleset)
        (self.repo / ".graphify_plus" / "drift_report.md").write_text(render_report(report))
        if on_event is not None:
            on_event(report)

    def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None


__all__ = ["DriftDaemon", "DriftReport", "check", "render_report"]
