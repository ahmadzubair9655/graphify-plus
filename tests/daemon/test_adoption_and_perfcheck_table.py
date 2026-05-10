"""Tests for the adoption-telemetry surface (gp daemon adoption) and the
12-cell perfcheck table — both review-guide-recommended."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.adoption import (
    AdoptionReport,
    adoption_report,
    grep_events_path,
    record_grep_event,
    render_report,
)
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.ops_rigor import (
    perfcheck_table,
    render_perf_table,
)
from graphify_plus.daemon.telemetry import telemetry_path
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd

# ---- adoption surface ---------------------------------------------------


def test_record_grep_event_writes_jsonl(tmp_path: Path) -> None:
    record_grep_event(
        tmp_path,
        pattern="AuthService",
        nudged=True,
        bareword=True,
        daemon_running=True,
        fresh=True,
        matched_node_count=3,
    )
    p = grep_events_path(tmp_path)
    assert p.exists()
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    assert rows[0]["pattern"] == "AuthService"
    assert rows[0]["nudged"] is True
    assert rows[0]["bareword"] is True


def test_adoption_report_no_data(tmp_path: Path) -> None:
    rep = adoption_report(tmp_path, window_hours=1)
    assert rep.graph_calls == 0
    assert rep.grep_calls == 0
    assert rep.adoption_rate == 0.0


def test_adoption_report_computes_rate(tmp_path: Path) -> None:
    # Seed three graph calls + one grep call.
    tel = telemetry_path(tmp_path)
    tel.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    tel.write_text(
        "\n".join(
            json.dumps(
                {
                    "ts": now,
                    "op": op,
                    "elapsed_ms": 1.0,
                    "tokens": 5,
                    "n_results": 1,
                    "trust": "FRESH",
                    "ok": True,
                }
            )
            for op in ("who_calls", "whats_in", "find_by_name")
        )
        + "\n",
        encoding="utf-8",
    )
    record_grep_event(
        tmp_path,
        pattern="AuthService",
        nudged=False,
        bareword=True,
        daemon_running=True,
        fresh=True,
        matched_node_count=2,
    )
    rep = adoption_report(tmp_path, window_hours=1)
    assert rep.graph_calls == 3
    assert rep.grep_calls == 1
    assert rep.adoption_rate == 0.75


def test_adoption_report_excludes_stale_events(tmp_path: Path) -> None:
    """Events older than the window are dropped from numerator + denominator."""
    p = grep_events_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    p.write_text(
        json.dumps(
            {
                "ts": old,
                "pattern": "x",
                "nudged": False,
                "bareword": True,
                "daemon_running": True,
                "fresh": True,
                "matched_node_count": 1,
            }
        )
        + "\n"
        + json.dumps(
            {
                "ts": new,
                "pattern": "y",
                "nudged": False,
                "bareword": True,
                "daemon_running": True,
                "fresh": True,
                "matched_node_count": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rep = adoption_report(tmp_path, window_hours=24)
    assert rep.grep_calls == 1


def test_adoption_report_top_missed(tmp_path: Path) -> None:
    """Top bareword greps that the daemon could have answered show up
    in top_missed."""
    for _ in range(3):
        record_grep_event(
            tmp_path,
            pattern="AuthService",
            nudged=False,
            bareword=True,
            daemon_running=True,
            fresh=True,
            matched_node_count=2,
        )
    record_grep_event(
        tmp_path,
        pattern="UserService",
        nudged=False,
        bareword=True,
        daemon_running=True,
        fresh=True,
        matched_node_count=1,
    )
    rep = adoption_report(tmp_path, window_hours=24)
    assert rep.top_missed
    assert rep.top_missed[0] == ("AuthService", 3)


def test_render_report_shapes(tmp_path: Path) -> None:
    rep = AdoptionReport(
        window_hours=24,
        graph_calls=10,
        grep_calls=5,
        adoption_rate=0.667,
        nudges_emitted=2,
        nudges_could_have=4,
        hook_potential_rate=0.8,
        top_missed=[("AuthService", 3)],
    )
    body = render_report(rep)
    assert "adoption" in body.lower()
    assert "AuthService" in body
    assert "✓ adoption target met" in body


def test_render_report_below_target(tmp_path: Path) -> None:
    rep = AdoptionReport(
        window_hours=24,
        graph_calls=2,
        grep_calls=10,
        adoption_rate=0.167,
        nudges_emitted=0,
        nudges_could_have=4,
        hook_potential_rate=0.4,
        top_missed=[],
    )
    body = render_report(rep)
    assert "below target" in body


def test_cli_adoption_no_data(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["adoption", "--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert "no events in window" in result.output


def test_nudge_accept_rate_counts_graph_within_window(tmp_path: Path) -> None:
    """A nudge followed by a graph call within NUDGE_ACCEPT_WINDOW_S
    counts as accepted; outside the window doesn't."""
    from graphify_plus.daemon.telemetry import telemetry_path

    now = datetime.now(timezone.utc)
    tel = telemetry_path(tmp_path)
    tel.parent.mkdir(parents=True, exist_ok=True)
    later = (now + timedelta(seconds=10)).isoformat()
    tel.write_text(
        json.dumps(
            {
                "ts": later,
                "op": "who_calls",
                "elapsed_ms": 1.0,
                "tokens": 5,
                "n_results": 1,
                "trust": "FRESH",
                "ok": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_grep_event(
        tmp_path,
        pattern="AuthService",
        nudged=True,
        bareword=True,
        daemon_running=True,
        fresh=True,
        matched_node_count=1,
    )
    rep = adoption_report(tmp_path, window_hours=24)
    assert rep.nudges_emitted == 1
    assert rep.nudges_followed_by_graph_call == 1
    assert rep.nudge_accept_rate == 1.0


def test_nudge_accept_rate_ignores_calls_before_nudge(tmp_path: Path) -> None:
    """A graph call BEFORE the nudge doesn't count as acceptance."""
    from graphify_plus.daemon.telemetry import telemetry_path

    now = datetime.now(timezone.utc)
    tel = telemetry_path(tmp_path)
    tel.parent.mkdir(parents=True, exist_ok=True)
    earlier = (now - timedelta(seconds=60)).isoformat()
    tel.write_text(
        json.dumps(
            {
                "ts": earlier,
                "op": "who_calls",
                "elapsed_ms": 1.0,
                "tokens": 5,
                "n_results": 1,
                "trust": "FRESH",
                "ok": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_grep_event(
        tmp_path,
        pattern="AuthService",
        nudged=True,
        bareword=True,
        daemon_running=True,
        fresh=True,
        matched_node_count=1,
    )
    rep = adoption_report(tmp_path, window_hours=24)
    assert rep.nudges_emitted == 1
    assert rep.nudges_followed_by_graph_call == 0


def test_baseline_write_load_roundtrip(tmp_path: Path) -> None:
    from graphify_plus.daemon.adoption import load_baseline, write_baseline

    rep = adoption_report(tmp_path)
    p = write_baseline(tmp_path, rep, label="test-v6.0")
    assert p.exists()
    loaded = load_baseline(tmp_path)
    assert loaded is not None
    assert loaded["label"] == "test-v6.0"
    assert loaded["adoption_rate"] == rep.adoption_rate


def test_baseline_delta_zero_when_first_capture(tmp_path: Path) -> None:
    record_grep_event(
        tmp_path,
        pattern="x",
        nudged=False,
        bareword=True,
        daemon_running=True,
        fresh=True,
        matched_node_count=1,
    )
    from graphify_plus.daemon.adoption import write_baseline

    rep = adoption_report(tmp_path)
    write_baseline(tmp_path, rep, label="v6.0")
    rep2 = adoption_report(tmp_path)
    assert rep2.baseline_adoption_rate == rep.adoption_rate
    assert rep2.delta_adoption_rate == 0.0


def test_baseline_delta_reflects_change(tmp_path: Path) -> None:
    """Capture baseline at low adoption, add graph calls, see positive delta."""
    from graphify_plus.daemon.adoption import write_baseline
    from graphify_plus.daemon.telemetry import telemetry_path

    record_grep_event(
        tmp_path,
        pattern="x",
        nudged=False,
        bareword=True,
        daemon_running=True,
        fresh=True,
        matched_node_count=1,
    )
    write_baseline(tmp_path, adoption_report(tmp_path), label="before")

    tel = telemetry_path(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    with tel.open("a", encoding="utf-8") as fp:
        for op in ("who_calls", "whats_in", "find_by_name"):
            fp.write(
                json.dumps(
                    {
                        "ts": now,
                        "op": op,
                        "elapsed_ms": 1.0,
                        "tokens": 5,
                        "n_results": 1,
                        "trust": "FRESH",
                        "ok": True,
                    }
                )
                + "\n"
            )
    rep = adoption_report(tmp_path)
    assert rep.baseline_adoption_rate == 0.0
    assert rep.adoption_rate == 0.75
    assert rep.delta_adoption_rate == 0.75


def test_cli_adoption_baseline_set_and_show(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd,
        ["adoption-baseline", "set", "--repo", str(tmp_path), "--label", "v6.0"],
    )
    assert result.exit_code == 0
    assert "baseline saved" in result.output
    show = runner.invoke(
        daemon_cmd, ["adoption-baseline", "show", "--repo", str(tmp_path), "--json"]
    )
    assert show.exit_code == 0
    body = json.loads(show.output)
    assert body["label"] == "v6.0"


def test_cli_adoption_baseline_show_missing(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["adoption-baseline", "show", "--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert "no baseline stored" in result.output


def test_render_report_shows_nudge_accept_and_baseline(tmp_path: Path) -> None:
    from graphify_plus.daemon.adoption import write_baseline

    record_grep_event(
        tmp_path,
        pattern="x",
        nudged=True,
        bareword=True,
        daemon_running=True,
        fresh=True,
        matched_node_count=1,
    )
    rep = adoption_report(tmp_path)
    write_baseline(tmp_path, rep, label="anchor")
    rep2 = adoption_report(tmp_path)
    body = render_report(rep2)
    assert "nudge accept" in body
    assert "vs baseline" in body


def test_cli_adoption_json(tmp_path: Path) -> None:
    record_grep_event(
        tmp_path,
        pattern="x",
        nudged=True,
        bareword=True,
        daemon_running=True,
        fresh=True,
        matched_node_count=1,
    )
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["adoption", "--repo", str(tmp_path), "--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["grep_calls"] == 1
    assert body["nudges_emitted"] == 1


# ---- 12-cell perfcheck table -------------------------------------------


def test_perfcheck_table_4x3_shape(snapshot: InMemoryGraph) -> None:
    table = perfcheck_table(snapshot, samples_per_cell=10)
    workloads = {c.workload for c in table.cells}
    states = {c.cache_state for c in table.cells}
    assert workloads == {"name_match", "concept_search", "1_hop", "multi_hop"}
    assert states == {"cold", "warm", "hot"}
    assert len(table.cells) == 12


def test_perfcheck_table_reports_p99_per_cell(snapshot: InMemoryGraph) -> None:
    table = perfcheck_table(snapshot, samples_per_cell=10)
    for c in table.cells:
        assert c.p99_ms >= c.p50_ms or c.samples == 1


def test_render_perf_table(snapshot: InMemoryGraph) -> None:
    table = perfcheck_table(snapshot, samples_per_cell=10)
    body = render_perf_table(table)
    assert "name_match" in body
    assert "concept_search" in body
    assert "1_hop" in body
    assert "multi_hop" in body
    assert "cold P99" in body and "warm P99" in body and "hot P99" in body


def test_perfcheck_table_target_pass(snapshot: InMemoryGraph) -> None:
    """On a tiny fixture every cell should be well under the 100ms P99 target."""
    table = perfcheck_table(snapshot, samples_per_cell=10)
    assert table.all_pass()


def test_cli_perfcheck_workload_all(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["perfcheck", "--repo", str(repo), "--workload", "all"])
    assert result.exit_code == 0
    assert "name_match" in result.output


def test_cli_perfcheck_workload_all_report(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["perfcheck", "--repo", str(repo), "--workload", "all", "--report"]
    )
    assert result.exit_code == 0
    assert "| workload |" in result.output


def test_cli_perfcheck_workload_all_json(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["perfcheck", "--repo", str(repo), "--workload", "all", "--json"]
    )
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["all_pass"] is True
    assert len(body["cells"]) == 12
