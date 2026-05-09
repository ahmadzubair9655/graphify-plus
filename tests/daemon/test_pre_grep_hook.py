"""End-to-end tests for ``pre_grep_hook.py`` (Layer 3.2/4.2).

The hook is the load-bearing piece of the entire master plan: it's the
file that intercepts a grep call and nudges Claude toward the graph.
If it breaks silently, the rewrite stops working in production.

We exercise it three ways:

1. **Direct function calls** — load the hook as a module via importlib,
   exercise `main()` and `_suggest()` against mocked stdin/stdout +
   mocked daemon client. Covers the decision logic.
2. **Subprocess invocation against no daemon** — confirms the hook
   stays silent when the daemon isn't reachable.
3. **Subprocess invocation against a real running daemon** — the
   actual production contract: a Claude Code PreToolUse JSON event
   in, a structured `decision: approve` advisory out.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from graphify_plus.daemon import DaemonClient, DaemonServer

HOOK_PATH = (
    Path(__file__).resolve().parents[2]
    / "graphify_plus"
    / "daemon"
    / "templates"
    / "pre_grep_hook.py"
)


def _load_hook():
    """Import the hook script as a module so coverage tracks it."""
    spec = importlib.util.spec_from_file_location("pre_grep_hook", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hook_module():
    return _load_hook()


@pytest.fixture
def background_daemon(repo: Path):
    server = DaemonServer(repo, watch=False)
    server.load_initial()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = DaemonClient(repo)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not client.is_running():
        time.sleep(0.02)
    if not client.is_running():
        server.stop()
        pytest.fail("daemon did not become ready")
    try:
        yield repo
    finally:
        server.stop()
        thread.join(timeout=2.0)


# ---- Direct unit tests ---------------------------------------------------


def test_bareword_regex_accepts_simple_names(hook_module) -> None:
    assert hook_module.BAREWORD.match("AuthService")
    assert hook_module.BAREWORD.match("snake_case_name")
    assert hook_module.BAREWORD.match("_leading_underscore")
    assert hook_module.BAREWORD.match("CamelCase42")


def test_bareword_regex_rejects_regex_patterns(hook_module) -> None:
    for bad in (
        "auth.*",
        "Auth\\..*Service",
        "(get|post)",
        "config\\[",
        "*.py",
        "src/",
        "",
    ):
        assert not hook_module.BAREWORD.match(bad), f"should reject {bad!r}"


def test_main_passes_through_invalid_json(hook_module, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = hook_module.main()
    assert rc == 0
    assert out.getvalue() == ""


def test_main_passes_through_non_grep_tool(hook_module, monkeypatch) -> None:
    event = {"tool_name": "Read", "tool_input": {"pattern": "AuthService"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = hook_module.main()
    assert rc == 0
    assert out.getvalue() == ""


def test_main_passes_through_regex_pattern(hook_module, monkeypatch) -> None:
    event = {"tool_name": "Grep", "tool_input": {"pattern": "auth\\..*Service"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = hook_module.main()
    assert rc == 0
    assert out.getvalue() == ""


def test_main_passes_through_empty_pattern(hook_module, monkeypatch) -> None:
    event = {"tool_name": "Grep", "tool_input": {"pattern": ""}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = hook_module.main()
    assert rc == 0
    assert out.getvalue() == ""


def test_main_emits_decision_when_suggest_fires(hook_module, monkeypatch, tmp_path) -> None:
    event = {"tool_name": "Grep", "tool_input": {"pattern": "AuthService"}, "cwd": str(tmp_path)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(
        hook_module,
        "_suggest_with_diagnostics",
        lambda repo, name: ("fake hint", True, True, 1),
    )
    rc = hook_module.main()
    assert rc == 0
    body = json.loads(out.getvalue())
    assert body["decision"] == "approve"
    assert body["reason"] == "fake hint"
    assert body["additionalContext"] == "fake hint"


def test_main_glob_tool_also_triggers(hook_module, monkeypatch, tmp_path) -> None:
    event = {"tool_name": "Glob", "tool_input": {"pattern": "AuthService"}, "cwd": str(tmp_path)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(
        hook_module,
        "_suggest_with_diagnostics",
        lambda repo, name: ("glob hint", True, True, 1),
    )
    rc = hook_module.main()
    assert rc == 0
    assert "glob hint" in out.getvalue()


def test_suggest_returns_none_when_daemon_down(hook_module, tmp_path: Path) -> None:
    """No daemon → silent. The hook must never fail loud."""
    out = hook_module._suggest(tmp_path, "AuthService")
    assert out is None


def test_suggest_silent_when_daemon_returns_no_results(hook_module, tmp_path: Path) -> None:
    """Daemon up but no matches → silent. Hook only nudges with real evidence."""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def is_running(self):
            return True

        def call(self, op, args):
            return {"freshness": {"trust": "FRESH"}, "results": []}

    with patch("graphify_plus.daemon.client.DaemonClient", FakeClient):
        out = hook_module._suggest(tmp_path, "DefinitelyNotAName")
    assert out is None


def test_suggest_silent_when_graph_stale(hook_module, tmp_path: Path) -> None:
    """Stale graphs lose the right to advise (Layer 3.2 contract)."""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def is_running(self):
            return True

        def call(self, op, args):
            return {
                "freshness": {"trust": "STALE_FILES"},
                "results": [
                    {
                        "label": "AuthService",
                        "source_file": "auth.py",
                        "line_number": 7,
                    }
                ],
            }

    with patch("graphify_plus.daemon.client.DaemonClient", FakeClient):
        out = hook_module._suggest(tmp_path, "AuthService")
    assert out is None


def test_suggest_fires_on_fresh_with_results(hook_module, tmp_path: Path) -> None:
    """The happy path: FRESH graph + real hits → structured advisory."""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def is_running(self):
            return True

        def call(self, op, args):
            return {
                "freshness": {"trust": "FRESH"},
                "results": [
                    {
                        "label": "auth.AuthService",
                        "source_file": "src/auth.py",
                        "line_number": 7,
                    }
                ],
            }

    with patch("graphify_plus.daemon.client.DaemonClient", FakeClient):
        out = hook_module._suggest(tmp_path, "AuthService")
    assert out is not None
    assert "AuthService" in out
    assert "src/auth.py:7" in out
    assert "gp_who_calls" in out


def test_suggest_fires_on_live_ahead_too(hook_module, tmp_path: Path) -> None:
    """LIVE_AHEAD trust also qualifies — it's still 'graph reflects reality'."""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def is_running(self):
            return True

        def call(self, op, args):
            return {
                "freshness": {"trust": "LIVE_AHEAD"},
                "results": [{"label": "x", "source_file": "x.py", "line_number": 1}],
            }

    with patch("graphify_plus.daemon.client.DaemonClient", FakeClient):
        out = hook_module._suggest(tmp_path, "x")
    assert out is not None


def test_suggest_swallows_daemon_exception(hook_module, tmp_path: Path) -> None:
    """If the daemon call raises mid-flight, the hook returns None — never blocks."""

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def is_running(self):
            return True

        def call(self, op, args):
            raise RuntimeError("transient")

    with patch("graphify_plus.daemon.client.DaemonClient", FakeClient):
        out = hook_module._suggest(tmp_path, "x")
    assert out is None


# ---- Subprocess tests against a real daemon -----------------------------


def _hook_env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }


def test_subprocess_silent_when_daemon_down(tmp_path: Path) -> None:
    event = json.dumps(
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "AuthService"},
            "cwd": str(tmp_path),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=event,
        capture_output=True,
        text=True,
        timeout=5,
        env=_hook_env(),
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_subprocess_silent_on_invalid_json(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="not valid json",
        capture_output=True,
        text=True,
        timeout=5,
        env=_hook_env(),
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_subprocess_silent_on_regex_pattern(tmp_path: Path) -> None:
    event = json.dumps(
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "auth\\..*Service"},
            "cwd": str(tmp_path),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=event,
        capture_output=True,
        text=True,
        timeout=5,
        env=_hook_env(),
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_subprocess_nudges_with_real_daemon(background_daemon: Path) -> None:
    """The actual production contract: real daemon up, real grep payload,
    structured advisory out. This is the test that proves the rewrite
    catches grep calls in practice.
    """
    event = json.dumps(
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "AuthService"},
            "cwd": str(background_daemon),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=event,
        capture_output=True,
        text=True,
        timeout=10,
        env=_hook_env(),
    )
    assert proc.returncode == 0
    body = json.loads(proc.stdout)
    assert body["decision"] == "approve"
    assert "AuthService" in body["reason"]
    assert "auth.py" in body["reason"]
    assert "gp_who_calls" in body["reason"] or "gp_find_by_name" in body["reason"]


def test_subprocess_camelcase_alias(background_daemon: Path) -> None:
    """Alternate field name 'toolName' (camelCase) is also accepted."""
    event = json.dumps(
        {
            "toolName": "Grep",
            "tool_input": {"pattern": "AuthService"},
            "cwd": str(background_daemon),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=event,
        capture_output=True,
        text=True,
        timeout=10,
        env=_hook_env(),
    )
    assert proc.returncode == 0
    body = json.loads(proc.stdout) if proc.stdout.strip() else {}
    assert body.get("decision") == "approve"


def test_subprocess_silent_for_unknown_pattern(background_daemon: Path) -> None:
    """No matches in the graph → no nudge. Don't fabricate hits."""
    event = json.dumps(
        {
            "tool_name": "Grep",
            "tool_input": {"pattern": "DefinitelyNotAName"},
            "cwd": str(background_daemon),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=event,
        capture_output=True,
        text=True,
        timeout=10,
        env=_hook_env(),
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
