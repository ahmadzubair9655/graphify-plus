"""End-to-end CLI tests for ``gp daemon``.

We invoke the click command in-process via ``CliRunner`` rather than
shelling out — same behaviour, but the test runs in a single Python
process so coverage tools see it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from graphify_plus.daemon import DaemonClient, DaemonServer
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd


@pytest.fixture
def background_daemon(repo: Path):
    server = DaemonServer(repo)
    server.load_initial()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = DaemonClient(repo)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if client.is_running():
            break
        time.sleep(0.02)
    else:
        server.stop()
        thread.join(timeout=1.0)
        pytest.fail("daemon did not become ready")
    try:
        yield server
    finally:
        server.stop()
        thread.join(timeout=2.0)


def test_status_when_not_running(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["status", "--repo", str(repo)])
    assert result.exit_code == 1
    assert "not running" in result.output


def test_status_when_running(background_daemon: DaemonServer, repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["status", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "running" in result.output
    assert "symbols" in result.output


def test_query_human_readable(background_daemon: DaemonServer, repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd,
        [
            "query",
            "whats_in",
            "--repo",
            str(repo),
            "--arg",
            "path=auth.py",
            "--receipt",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "trust:" in result.output
    assert "auth.py" in result.output
    assert "[graphify-plus]" in result.output


def test_query_json(background_daemon: DaemonServer, repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd,
        [
            "query",
            "whats_in",
            "--repo",
            str(repo),
            "--arg",
            "path=auth.py",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    import json

    body = json.loads(result.output)
    assert body["ok"] is True
    assert body["results"]


def test_query_when_daemon_down(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd,
        ["query", "whats_in", "--repo", str(repo), "--arg", "path=auth.py"],
    )
    assert result.exit_code != 0
    assert "daemon not running" in result.output


def test_refresh_when_daemon_down(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["refresh", "--repo", str(repo)])
    assert result.exit_code != 0


def test_refresh_when_running(background_daemon: DaemonServer, repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["refresh", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    assert "refreshed" in result.output
