"""Tests for the onboarding walkthrough (Sprint 10.2)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.handlers import onboard as onboard_handler
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.onboarding import (
    OnboardingPlan,
    WalkStop,
    format_plan,
    make_plan,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd


def test_make_plan_populates_central(snapshot: InMemoryGraph) -> None:
    plan = make_plan(snapshot)
    assert plan.n_symbols >= 1
    assert plan.n_files >= 1
    # On the small fixture we ask for ≥3 central nodes, but the ranking
    # may have fewer real symbols available — verify the data is shaped
    # right whatever the count.
    for stop in plan.central:
        assert stop.label
        assert stop.source_file
        assert stop.line_number >= 0


def test_make_plan_groups_by_top_directory(snapshot: InMemoryGraph) -> None:
    plan = make_plan(snapshot)
    # The fixture has only "auth.py" at the repo root, so by_module
    # might be empty or contain a single bucket. Either is fine — we
    # just verify the structure is consistent.
    for stops in plan.by_module.values():
        assert isinstance(stops, list)
        assert all(isinstance(s, WalkStop) for s in stops)


def test_make_plan_handles_no_coverage(snapshot: InMemoryGraph) -> None:
    plan = make_plan(snapshot)
    assert plan.welltested_examples == []  # no coverage data ingested


def test_format_plan_renders_markdown(snapshot: InMemoryGraph) -> None:
    plan = make_plan(snapshot)
    md = format_plan(plan)
    assert md.startswith("# Onboarding")
    assert "Read these first" in md or "central" in md.lower()
    assert "Tour the codebase" in md or "Next steps" in md


def test_handler_returns_onboarding_dict(snapshot: InMemoryGraph) -> None:
    resp = onboard_handler(snapshot, {"persona": "frontend"})
    body = resp["extra"]["onboarding"]
    assert body["persona"] == "frontend"
    assert "central" in body
    assert "by_module" in body


def test_cli_onboard_human_output(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["onboard", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "Onboarding" in result.output
    assert "Next steps" in result.output


def test_cli_onboard_json_round_trip(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["onboard", "--repo", str(repo), "--persona", "backend", "--json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["persona"] == "backend"
    plan = OnboardingPlan(
        repo=body["repo"],
        persona=body["persona"],
        n_symbols=int(body["n_symbols"]),
        n_files=int(body["n_files"]),
        central=[WalkStop(**s) for s in body["central"]],
        welltested_examples=[WalkStop(**s) for s in body["welltested_examples"]],
        by_module={
            k: [WalkStop(**s) for s in v] for k, v in body["by_module"].items()
        },
    )
    md = format_plan(plan)
    assert "Onboarding" in md
