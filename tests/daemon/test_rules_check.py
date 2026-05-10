"""Tests for ``rules_check`` daemon op + ``gp daemon rules check`` (Sprint 9.4)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.handlers import rules_check
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd


def test_rules_check_no_ruleset(snapshot: InMemoryGraph) -> None:
    resp = rules_check(snapshot, {})
    assert resp["results"] == []
    assert resp["extra"]["rule_count"] == 0
    assert "no ruleset" in resp["extra"]["reason"]


def test_rules_check_returns_violations(snapshot: InMemoryGraph, repo: Path) -> None:
    rules_path = repo / ".graphify_plus" / "rules.yaml"
    rules_path.write_text(
        textwrap.dedent(
            """\
            layers:
              app: ["auth.py"]
              external: ["**"]
            rules:
              - id: no-self-calls
                description: dummy rule that flags every calls edge
                forbid_edge:
                  kinds: [calls]
                severity: warning
            """
        )
    )
    snapshot.repo_root = repo  # ensure handler reads the right rules.yaml
    resp = rules_check(snapshot, {})
    extra = resp["extra"]
    assert extra["rule_count"] == 1
    # The fixture has at least one calls edge, so a forbid_edge rule should fire.
    assert extra["violation_count"] >= 1
    rows = resp["results"]
    assert all(r.get("rule_id") == "no-self-calls" for r in rows)
    # Each row that points at a real symbol carries file:line.
    real = [r for r in rows if r.get("src") and r["src"].get("source_file")]
    assert real
    for row in real:
        assert row["src"]["line_number"] >= 0


def test_rules_check_grade_letter(snapshot: InMemoryGraph, repo: Path, tmp_path: Path) -> None:
    rules_path = repo / ".graphify_plus" / "rules.yaml"
    rules_path.write_text(
        textwrap.dedent(
            """\
            layers: {app: ["auth.py"]}
            rules: []
            """
        )
    )
    snapshot.repo_root = repo
    resp = rules_check(snapshot, {})
    # Empty ruleset → no violations → grade A.
    assert resp["extra"]["grade"] == "A"


def test_rules_check_invalid_yaml_returns_error(snapshot: InMemoryGraph, repo: Path) -> None:
    rules_path = repo / ".graphify_plus" / "rules.yaml"
    rules_path.write_text("layers:\n  - this is not\n  valid yaml: [")
    snapshot.repo_root = repo
    resp = rules_check(snapshot, {})
    assert "error" in resp
    assert resp["error"]["code"] == "BAD_REQUEST"


def test_cli_rules_check_no_ruleset(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["rules", "check", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "no ruleset" in result.output


def test_cli_rules_check_with_violations(repo: Path) -> None:
    rules_path = repo / ".graphify_plus" / "rules.yaml"
    rules_path.write_text(
        textwrap.dedent(
            """\
            layers:
              app: ["auth.py"]
            rules:
              - id: forbid-all-calls
                forbid_edge: {kinds: [calls]}
                severity: error
            """
        )
    )
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["rules", "check", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "forbid-all-calls" in result.output


def test_cli_rules_check_fail_on_error(repo: Path) -> None:
    rules_path = repo / ".graphify_plus" / "rules.yaml"
    rules_path.write_text(
        textwrap.dedent(
            """\
            layers:
              app: ["auth.py"]
            rules:
              - id: forbid-all-calls
                forbid_edge: {kinds: [calls]}
                severity: error
            """
        )
    )
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd,
        ["rules", "check", "--repo", str(repo), "--fail-on-error"],
    )
    assert result.exit_code == 1


def test_cli_rules_check_json_output(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["rules", "check", "--repo", str(repo), "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert "extra" in body
    assert "violations" in body
