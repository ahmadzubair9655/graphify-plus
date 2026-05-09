"""Tests for Batch G: Layers 11.1 (team), 13.1 (session), 13.2 (pre-edit), 14.3 (benchmark)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.benchmark import (
    BenchmarkCheck,
    BenchmarkEntry,
    builtin_tiny_corpus,
    load_corpus,
    render_report,
    run_check,
    run_entry,
)
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.session_manager import (
    pre_edit,
    session_status,
)
from graphify_plus.daemon.team import (
    add_member,
    annotations_by_target,
    detect_conflicts,
    init_team,
    list_shared_queries,
    load_manifest,
    pull_annotations,
    push_annotation,
    push_query,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd


# ---- Layer 11.1 — team graph -------------------------------------------


def test_init_team_creates_layout(tmp_path: Path) -> None:
    cfg = init_team(tmp_path / "team", name="alpha")
    assert (cfg.root / "manifest.json").exists()
    assert (cfg.root / "shared-queries").exists()
    body = load_manifest(cfg.root)
    assert body["name"] == "alpha"


def test_add_member(tmp_path: Path) -> None:
    cfg = init_team(tmp_path / "team")
    body = add_member(cfg.root, "alice")
    assert "alice" in body["members"]


def test_push_pull_annotations(tmp_path: Path) -> None:
    cfg = init_team(tmp_path / "team")
    push_annotation(cfg.root, target="sid-1", note="x", author="a")
    push_annotation(cfg.root, target="sid-2", note="y", author="b")
    rows = pull_annotations(cfg.root)
    assert len(rows) == 2


def test_push_annotation_idempotent(tmp_path: Path) -> None:
    cfg = init_team(tmp_path / "team")
    push_annotation(cfg.root, target="sid-1", note="x", author="a")
    push_annotation(cfg.root, target="sid-1", note="x", author="a")
    assert len(pull_annotations(cfg.root)) == 1


def test_detect_conflicts(tmp_path: Path) -> None:
    cfg = init_team(tmp_path / "team")
    push_annotation(cfg.root, target="sid-1", note="left", author="alice")
    push_annotation(cfg.root, target="sid-1", note="right", author="bob")
    rows = pull_annotations(cfg.root)
    conflicts = detect_conflicts(rows)
    assert conflicts
    assert conflicts[0]["target"] == "sid-1"


def test_push_and_list_shared_queries(tmp_path: Path) -> None:
    cfg = init_team(tmp_path / "team")
    push_query(cfg.root, "untested", "FIND nodes WHERE test_coverage < 0.1")
    rows = list_shared_queries(cfg.root)
    assert any(r["name"] == "untested" for r in rows)


def test_annotations_by_target(tmp_path: Path) -> None:
    cfg = init_team(tmp_path / "team")
    push_annotation(cfg.root, target="sid-1", note="x", author="a")
    push_annotation(cfg.root, target="sid-2", note="y", author="b")
    grouped = annotations_by_target(cfg.root)
    assert "sid-1" in grouped
    assert "sid-2" in grouped


def test_cli_team_init(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["team", "init", str(tmp_path / "team")]
    )
    assert result.exit_code == 0
    assert (tmp_path / "team" / "manifest.json").exists()


def test_cli_team_push_pull(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(daemon_cmd, ["team", "init", str(tmp_path / "team")])
    push = runner.invoke(
        daemon_cmd,
        ["team", "push-annotation", "sid-1", "hello", "--root", str(tmp_path / "team"), "--author", "alice"],
    )
    assert push.exit_code == 0
    pull = runner.invoke(
        daemon_cmd,
        ["team", "pull", "--root", str(tmp_path / "team")],
    )
    assert pull.exit_code == 0
    assert "alice" in pull.output


# ---- Layer 13.1 / 13.2 — session + pre-edit ----------------------------


def test_session_status_no_daemon(repo: Path) -> None:
    state = session_status(repo)
    assert state.daemon_running is False
    assert state.claude_md_present is False or state.claude_md_present is True


def test_pre_edit_finds_target(snapshot: InMemoryGraph) -> None:
    rep = pre_edit(snapshot, "AuthService.login")
    assert rep.affected
    assert rep.affected[0]["label"]


def test_pre_edit_unknown_target_notes(snapshot: InMemoryGraph) -> None:
    rep = pre_edit(snapshot, "doesnotexist")
    assert any("not found" in n for n in rep.notes)


def test_pre_edit_low_coverage_notes(snapshot: InMemoryGraph) -> None:
    target_sid = next(iter(snapshot.by_id))
    snapshot.coverage[target_sid] = {"pct": 0.05, "lines_covered": 1, "lines_total": 20, "source": "x"}
    sym = snapshot.by_id[target_sid]
    rep = pre_edit(snapshot, sym.get("name") or sym.get("qualified_name") or "?")
    assert any("low coverage" in n for n in rep.notes)


def test_cli_session_status(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["session-status", "--repo", str(repo)])
    assert result.exit_code == 0
    assert "daemon_running" in result.output


def test_cli_pre_edit(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["pre-edit", "AuthService.login", "--repo", str(repo)]
    )
    assert result.exit_code == 0
    assert "pre-edit ritual" in result.output


# ---- Layer 14.3 — benchmark harness ----------------------------------


def test_run_check_passes_when_threshold_met(snapshot: InMemoryGraph) -> None:
    chk = BenchmarkCheck(op="find_by_name", args={"label": "auth"}, expect_at_least=1)
    res = run_check(snapshot, chk)
    assert res.passed
    assert res.actual_count >= 1


def test_run_check_fails_unknown_op(snapshot: InMemoryGraph) -> None:
    chk = BenchmarkCheck(op="not_a_real_op", expect_at_least=1)
    res = run_check(snapshot, chk)
    assert not res.passed


def test_run_entry_aggregates(snapshot: InMemoryGraph) -> None:
    entry = BenchmarkEntry(
        name="x",
        repo_path="<x>",
        checks=[
            BenchmarkCheck(op="find_by_name", args={"label": "auth"}, expect_at_least=1),
            BenchmarkCheck(op="who_calls", args={"node": "AuthService.login"}, expect_at_least=1),
        ],
    )
    rep = run_entry(entry, snapshot)
    assert rep.total == 2
    assert rep.precision() > 0


def test_load_corpus(tmp_path: Path) -> None:
    p = tmp_path / "corpus.json"
    p.write_text(
        json.dumps(
            [
                {
                    "name": "x",
                    "repo_path": "<x>",
                    "checks": [{"op": "find_by_name", "args": {"label": "y"}, "expect_at_least": 1}],
                }
            ]
        )
    )
    rows = load_corpus(p)
    assert rows[0].name == "x"
    assert rows[0].checks[0].expect_at_least == 1


def test_render_report() -> None:
    from graphify_plus.daemon.benchmark import BenchmarkReport, CheckResult

    rep = BenchmarkReport(
        name="x",
        total=2,
        passed=1,
        results=[
            CheckResult(op="op1", passed=True, actual_count=3),
            CheckResult(op="op2", passed=False, actual_count=0, notes="missing"),
        ],
    )
    body = render_report([rep])
    assert "1/2 passed" in body
    assert "✓" in body
    assert "✗" in body


def test_builtin_tiny_corpus() -> None:
    rows = builtin_tiny_corpus()
    assert rows
    assert rows[0].name == "auth-fixture"


def test_cli_benchmark(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["benchmark", "--repo", str(repo)])
    assert result.exit_code == 0
    assert "benchmark report" in result.output.lower()
