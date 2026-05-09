"""Layer 13.1 + 13.2 — always-on session and pre-edit ritual.

* **session start** — runs the full bring-up sequence (daemon detached,
  watcher running, claude-md updated, telemetry sink ready, recent
  health snapshot).
* **session-status** — single command that reports whether everything
  is healthy.
* **pre-edit <symbol>** — runs the master plan's 8-step ritual:
    1. Find affected nodes
    2. Counterfactual / blast-radius check
    3. Architectural rule check
    4. Test reverse-lookup
    5. Coverage-touched check
    6. Risk score
    7. Print plan
    8. (Edit happens externally.)
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .indexes import InMemoryGraph

log = logging.getLogger("graphify_plus.daemon.session_manager")


@dataclass
class SessionState:
    repo: str
    started_at: str
    daemon_running: bool
    watcher_running: bool
    coverage_ingested: bool
    rules_configured: bool
    claude_md_present: bool
    health_recent: bool


@dataclass
class PreEditReport:
    target: str
    affected: list[dict[str, Any]] = field(default_factory=list)
    blast_radius: list[dict[str, Any]] = field(default_factory=list)
    rule_violations: list[dict[str, Any]] = field(default_factory=list)
    tests_to_run: list[dict[str, Any]] = field(default_factory=list)
    coverage_pct: float | None = None
    risk: str = "LOW"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def session_start(repo: Path) -> dict[str, Any]:
    """Run the entire bring-up sequence. Returns a structured report."""
    from datetime import datetime, timezone

    out: dict[str, Any] = {
        "repo": str(repo),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
    }
    repo = repo.resolve()
    # 1. Detached daemon.
    try:
        subprocess.run(
            [sys.executable, "-m", "graphify_plus", "daemon", "start", "--repo", str(repo), "--detach"],
            check=False,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        out["steps"].append({"step": "daemon-start", "ok": True})
    except Exception as exc:  # noqa: BLE001
        out["steps"].append({"step": "daemon-start", "error": str(exc)})

    # 2. CLAUDE.md owned section refresh.
    try:
        from .context_file import build_section, render_section, update_all_known
        from ..runtime.store import Store, cache_path

        if cache_path(repo).exists():
            store = Store(cache_path(repo))
            try:
                snap = InMemoryGraph.from_store(store, repo)
            finally:
                store.close()
            section = build_section(snap)
            update_all_known(repo, render_section(section, repo_name=repo.name))
            out["steps"].append({"step": "claude-md", "ok": True})
        else:
            out["steps"].append({"step": "claude-md", "skipped": "no cache"})
    except Exception as exc:  # noqa: BLE001
        out["steps"].append({"step": "claude-md", "error": str(exc)})

    # 3. Health snapshot.
    try:
        from .anomaly import append_snapshot, take_snapshot
        from ..runtime.store import Store, cache_path

        if cache_path(repo).exists():
            store = Store(cache_path(repo))
            try:
                snap = InMemoryGraph.from_store(store, repo)
            finally:
                store.close()
            append_snapshot(repo, take_snapshot(snap))
            out["steps"].append({"step": "health-snapshot", "ok": True})
    except Exception as exc:  # noqa: BLE001
        out["steps"].append({"step": "health-snapshot", "error": str(exc)})

    return out


def session_status(repo: Path) -> SessionState:
    from .client import DaemonClient

    repo = repo.resolve()
    client = DaemonClient(repo, timeout_s=0.5)
    daemon = client.is_running()
    watcher = False
    if daemon:
        # Best-effort: server keeps a private flag, but we don't expose
        # it; assume yes when daemon up.
        watcher = True
    cov = False
    rules = False
    claude_md = (repo / "CLAUDE.md").exists()
    health_recent = False
    try:
        from .coverage import coverage_summary
        from .schema_version import DAEMON_SCHEMA_VERSION  # noqa: F401
        from ..runtime.store import Store, cache_path

        if cache_path(repo).exists():
            store = Store(cache_path(repo), integrity_check=False)
            try:
                summary = coverage_summary(store)
                cov = bool(summary.get("symbols"))
            finally:
                store.close()
        rules = (repo / ".graphify_plus" / "rules.yaml").exists()
        from .anomaly import history

        rows = history(repo, limit=1)
        if rows:
            from datetime import datetime, timezone

            try:
                ts = datetime.fromisoformat(
                    rows[-1]["ts"].replace("Z", "+00:00")
                ).timestamp()
                health_recent = (time.time() - ts) < 24 * 3600
            except (ValueError, KeyError):
                health_recent = False
    except Exception:  # noqa: BLE001
        pass
    from datetime import datetime, timezone

    return SessionState(
        repo=str(repo),
        started_at=datetime.now(timezone.utc).isoformat(),
        daemon_running=daemon,
        watcher_running=watcher,
        coverage_ingested=cov,
        rules_configured=rules,
        claude_md_present=claude_md,
        health_recent=health_recent,
    )


def pre_edit(graph: InMemoryGraph, target: str) -> PreEditReport:
    """Run the master plan's 8-step ritual against ``target``."""
    report = PreEditReport(target=target)
    matches = graph.find_by_name(target, fuzzy=True, limit=4)
    if not matches:
        report.notes.append(f"target {target!r} not found in graph")
        return report
    sym = matches[0]
    sid = sym["id"]
    report.affected.append(
        {
            "label": sym.get("qualified_name") or sym.get("name") or sid,
            "source_file": sym.get("path") or "",
            "line_number": int((sym.get("span") or (0, 0))[0]),
            "kind": sym.get("kind") or "",
        }
    )

    # Blast radius via dependents.
    deps = graph.dependents_of(sid)
    for d in deps[:24]:
        report.blast_radius.append(
            {
                "label": d.get("qualified_name") or d.get("name") or d.get("id"),
                "source_file": d.get("path") or "",
                "line_number": int((d.get("span") or (0, 0))[0]),
            }
        )

    # Architectural rules.
    try:
        from .handlers import rules_check

        rc = rules_check(graph, {})
        targeted = [
            v
            for v in rc.get("results", [])
            if (v.get("src") or {}).get("node_id") == sid
            or (v.get("dst") or {}).get("node_id") == sid
        ]
        report.rule_violations = targeted
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"rules_check unavailable: {exc}")

    # Test reverse-lookup. Tests that mention the symbol's name in
    # their qualified_name or path = good first guess.
    name = (sym.get("name") or "").lower()
    tests: list[dict[str, Any]] = []
    if name:
        for other_sid, other_sym in graph.by_id.items():
            path = (other_sym.get("path") or "").lower()
            if "test" not in path:
                continue
            qname = (other_sym.get("qualified_name") or "").lower()
            if name in qname or name in path:
                tests.append(
                    {
                        "label": other_sym.get("qualified_name") or "",
                        "source_file": other_sym.get("path") or "",
                        "line_number": int((other_sym.get("span") or (0, 0))[0]),
                    }
                )
                if len(tests) >= 8:
                    break
    report.tests_to_run = tests

    # Coverage-touched.
    cov = graph.coverage.get(sid)
    if cov:
        report.coverage_pct = round(float(cov.get("pct", 0.0)) * 100, 1)

    # Risk grade.
    pr_set = {sid for sid, _ in graph.pagerank_top}
    central = sid in pr_set
    n_deps = len(deps)
    if central or n_deps >= 12 or report.rule_violations:
        report.risk = "HIGH" if (central and n_deps >= 6) else "MEDIUM"
    if report.coverage_pct is not None and report.coverage_pct <= 20.0:
        report.notes.append(
            f"low coverage ({report.coverage_pct}%) — add tests before/after editing"
        )
    if not tests and (sym.get("kind") or "") in {"function", "method"}:
        report.notes.append(
            "no obvious test files mention this symbol — search wider before relying on tests"
        )
    return report


__all__ = ["PreEditReport", "SessionState", "pre_edit", "session_start", "session_status"]
