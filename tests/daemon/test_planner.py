"""Tests for the graph-grounded edit planner (Sprint 6)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.handlers import plan as plan_handler
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.planner import Plan, format_plan, make_plan
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd


def test_make_plan_finds_affected_nodes(snapshot: InMemoryGraph) -> None:
    p = make_plan(snapshot, "authenticate user login flow")
    assert p.affected
    labels = [n.label for n in p.affected]
    assert any("auth" in lab.lower() for lab in labels)
    for n in p.affected:
        assert n.source_file
        assert n.line_number >= 0


def test_make_plan_handles_empty_task(snapshot: InMemoryGraph) -> None:
    p = make_plan(snapshot, "")
    assert p.affected == []
    assert p.notes  # we surface a useful "why empty" message


def test_make_plan_unmatched_task(snapshot: InMemoryGraph) -> None:
    p = make_plan(snapshot, "quantum cryptography blockchain")
    assert p.affected == []
    assert any("no symbols" in note.lower() for note in p.notes)


def test_plan_risk_grade_low_for_isolated_change(snapshot: InMemoryGraph) -> None:
    p = make_plan(snapshot, "validate password")
    assert p.risk in ("LOW", "MEDIUM", "HIGH")


def test_plan_token_estimates_present(snapshot: InMemoryGraph) -> None:
    p = make_plan(snapshot, "authenticate user")
    assert p.estimated_tokens_graph > 0
    assert p.estimated_tokens_grep > p.estimated_tokens_graph, (
        "the whole point: graph context is cheaper than grep-only research"
    )


def test_format_plan_renders_markdown(snapshot: InMemoryGraph) -> None:
    p = make_plan(snapshot, "authenticate user")
    out = format_plan(p)
    assert out.startswith("# Plan —")
    assert "**Risk**" in out
    assert "**Affected nodes**" in out
    assert "tokens" in out


def test_plan_handler_returns_dict(snapshot: InMemoryGraph) -> None:
    resp = plan_handler(snapshot, {"task": "authenticate user"})
    assert resp["results"] == []
    payload = resp["extra"]["plan"]
    assert "affected" in payload
    assert "blast_radius" in payload
    assert "risk" in payload


def test_plan_cli_human_output(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["plan", "authenticate user login", "--repo", str(repo)]
    )
    assert result.exit_code == 0, result.output
    assert "Plan —" in result.output
    assert "Risk" in result.output


def test_plan_cli_json_output(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["plan", "authenticate user", "--repo", str(repo), "--json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert "affected" in body
    assert "risk" in body


def test_plan_handler_no_task(snapshot: InMemoryGraph) -> None:
    resp = plan_handler(snapshot, {})
    assert resp["extra"]["reason"] == "no task given"


def test_plan_node_serialisation_round_trip(snapshot: InMemoryGraph) -> None:
    p = make_plan(snapshot, "authenticate user")
    d = p.to_dict()
    # Confirm round-trip into Plan + PlanNode dataclasses works (the CLI
    # uses this when the daemon returns the payload as JSON).
    from graphify_plus.daemon.planner import PlanNode

    rebuilt = Plan(
        task=d["task"],
        affected=[PlanNode(**n) for n in d["affected"]],
        blast_radius=[PlanNode(**n) for n in d["blast_radius"]],
        risk=d["risk"],
        risk_reasons=list(d["risk_reasons"]),
        estimated_tokens_graph=d["estimated_tokens_graph"],
        estimated_tokens_grep=d["estimated_tokens_grep"],
        notes=list(d["notes"]),
    )
    assert rebuilt.task == p.task
    assert len(rebuilt.affected) == len(p.affected)
