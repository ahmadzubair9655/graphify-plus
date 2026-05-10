"""Tests for the local telemetry sink (Sprint 5)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon import DaemonClient, DaemonServer
from graphify_plus.daemon.telemetry import (
    aggregate,
    append_event,
    telemetry_path,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd


def test_append_event_creates_file(tmp_path: Path) -> None:
    append_event(
        tmp_path,
        "who_calls",
        elapsed_ms=0.5,
        tokens=42,
        n_results=3,
        trust="FRESH",
        ok=True,
    )
    path = telemetry_path(tmp_path)
    assert path.exists()
    line = path.read_text().splitlines()[0]
    ev = json.loads(line)
    assert ev["op"] == "who_calls"
    assert ev["tokens"] == 42
    assert ev["trust"] == "FRESH"
    assert ev["ok"] is True
    assert ev["ts"]


def test_append_event_silently_swallows_errors(tmp_path: Path) -> None:
    """Telemetry failures must never propagate. Pointing at a path whose
    parent isn't writable simulates a permission-denied scenario.
    """
    bad = tmp_path / "nope_read_only" / "x"
    bad.parent.mkdir()
    bad.parent.chmod(0o500)
    try:
        # Should not raise.
        append_event(
            bad,
            "who_calls",
            elapsed_ms=0.5,
            tokens=42,
            n_results=3,
            trust="FRESH",
            ok=True,
        )
    finally:
        bad.parent.chmod(0o700)


def test_aggregate_empty_returns_zeros(tmp_path: Path) -> None:
    summary = aggregate(tmp_path)
    assert summary["total_calls"] == 0
    assert summary["ok_rate"] == 0.0
    assert summary["fresh_rate"] == 0.0
    assert summary["by_op"] == {}


def test_aggregate_groups_by_op(tmp_path: Path) -> None:
    for i in range(5):
        append_event(
            tmp_path,
            "who_calls",
            elapsed_ms=float(i),
            tokens=10 * i,
            n_results=i,
            trust="FRESH",
            ok=True,
        )
    for _ in range(2):
        append_event(
            tmp_path,
            "find_by_name",
            elapsed_ms=0.5,
            tokens=20,
            n_results=1,
            trust="STALE_FILES",
            ok=True,
        )
    append_event(
        tmp_path,
        "error",
        elapsed_ms=0.0,
        tokens=0,
        n_results=0,
        trust="FRESH",
        ok=False,
        error_code="NOT_FOUND",
    )
    summary = aggregate(tmp_path)
    assert summary["total_calls"] == 8
    assert summary["by_op"]["who_calls"]["calls"] == 5
    assert summary["by_op"]["find_by_name"]["calls"] == 2
    assert summary["by_op"]["error"]["calls"] == 1
    # 6 of 8 events were FRESH.
    assert summary["fresh_rate"] == round(6 / 8, 4)
    # 7 of 8 were ok.
    assert summary["ok_rate"] == round(7 / 8, 4)
    assert "top_errors" in summary
    codes = dict(summary["top_errors"])
    assert codes["NOT_FOUND"] == 1


def test_daemon_writes_telemetry(repo: Path) -> None:
    server = DaemonServer(repo, watch=False)
    server.load_initial()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = DaemonClient(repo)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not client.is_running():
        time.sleep(0.02)
    try:
        client.call("whats_in", {"path": "auth.py"})
        client.call("find_by_name", {"label": "auth"})
        # Trigger an error too.
        try:
            client.call("who_calls", {"node": "definitely_missing"})
        except Exception:
            pass
    finally:
        server.stop()
        thread.join(timeout=2.0)
    summary = aggregate(repo)
    assert summary["total_calls"] >= 3
    assert "whats_in" in summary["by_op"]
    assert summary["by_op"]["whats_in"]["calls"] >= 1
    assert summary["by_op"]["whats_in"]["fresh_rate"] == 1.0


def test_stats_cli_human_output(tmp_path: Path) -> None:
    append_event(
        tmp_path, "who_calls", elapsed_ms=1.0, tokens=10, n_results=1, trust="FRESH", ok=True
    )
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["stats", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "who_calls" in result.output
    assert "FRESH" in result.output


def test_stats_cli_json_output(tmp_path: Path) -> None:
    append_event(
        tmp_path, "who_calls", elapsed_ms=1.0, tokens=10, n_results=1, trust="FRESH", ok=True
    )
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["stats", "--repo", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["total_calls"] >= 1
    assert "who_calls" in body["by_op"]


def test_stats_cli_empty_repo(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["stats", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "no telemetry yet" in result.output
