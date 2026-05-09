"""Tests for ``gp daemon install`` — Layer 4 routing skill + pre-grep hook."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.interface.cli.daemon_cmd import daemon_cmd


def test_install_writes_skill_and_hook(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["install", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    skill = tmp_path / ".claude" / "skills" / "graphify-plus" / "SKILL.md"
    hook = tmp_path / ".claude" / "hooks" / "pre_grep_hook.py"
    assert skill.exists()
    assert hook.exists()
    # Hook is executable.
    mode = hook.stat().st_mode
    assert mode & stat.S_IXUSR
    # SKILL.md has the routing frontmatter so Claude picks it up.
    body = skill.read_text()
    assert body.startswith("---")
    assert "graphify-plus" in body
    assert "Default rule" in body


def test_install_skips_existing_without_force(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(daemon_cmd, ["install", "--repo", str(tmp_path)])
    skill = tmp_path / ".claude" / "skills" / "graphify-plus" / "SKILL.md"
    skill.write_text("# manual override\n")
    result = runner.invoke(daemon_cmd, ["install", "--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert "skipped" in result.output
    assert skill.read_text() == "# manual override\n"


def test_install_force_overwrites(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(daemon_cmd, ["install", "--repo", str(tmp_path)])
    skill = tmp_path / ".claude" / "skills" / "graphify-plus" / "SKILL.md"
    skill.write_text("# manual override\n")
    result = runner.invoke(daemon_cmd, ["install", "--repo", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert "installed" in result.output
    assert "graphify-plus" in skill.read_text()


def test_pre_grep_hook_silent_when_no_daemon(tmp_path: Path) -> None:
    """The hook must never block grep — and when no daemon is running it
    stays silent (no stdout)."""
    runner = CliRunner()
    runner.invoke(daemon_cmd, ["install", "--repo", str(tmp_path)])
    hook_path = tmp_path / ".claude" / "hooks" / "pre_grep_hook.py"
    event = json.dumps(
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "AuthService"},
            "cwd": str(tmp_path),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(hook_path)],
        input=event,
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    assert proc.returncode == 0
    # No daemon means no advice — stdout must be empty (or whitespace).
    assert proc.stdout.strip() == ""


def test_pre_grep_hook_nudges_when_daemon_fresh(repo: Path) -> None:
    """When the daemon is up and finds a structural hit, the hook
    surfaces a suggestion via the standard `decision: approve` shape.
    """
    import threading
    import time

    from graphify_plus.daemon import DaemonClient, DaemonServer

    runner = CliRunner()
    runner.invoke(daemon_cmd, ["install", "--repo", str(repo)])
    hook_path = repo / ".claude" / "hooks" / "pre_grep_hook.py"

    server = DaemonServer(repo, watch=False)
    server.load_initial()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = DaemonClient(repo)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not client.is_running():
        time.sleep(0.02)

    try:
        event = json.dumps(
            {
                "tool_name": "Grep",
                "tool_input": {"pattern": "AuthService"},
                "cwd": str(repo),
            }
        )
        proc = subprocess.run(
            [sys.executable, str(hook_path)],
            input=event,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        )
        assert proc.returncode == 0
        assert proc.stdout.strip(), "expected a suggestion when daemon is up"
        body = json.loads(proc.stdout)
        assert body["decision"] == "approve"
        assert "graphify-plus" in body["reason"].lower()
        assert "AuthService" in body["reason"]
    finally:
        server.stop()
        thread.join(timeout=3.0)


def test_pre_grep_hook_ignores_regex_patterns(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(daemon_cmd, ["install", "--repo", str(tmp_path)])
    hook_path = tmp_path / ".claude" / "hooks" / "pre_grep_hook.py"
    event = json.dumps(
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "auth\\..*Service"},
            "cwd": str(tmp_path),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(hook_path)],
        input=event,
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
