"""Tests for the MCP-side intent tools.

These tools are the bridge between Claude Code and the daemon. They must:

  * Route through the daemon when it's running.
  * Fall back to a direct in-process build when the daemon isn't running,
    so the user gets a usable answer either way.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from graphify_plus.daemon import DaemonClient, DaemonServer
from graphify_plus.interface.mcp_server import (
    tool_find_by_concept,
    tool_find_by_name,
    tool_what_depends_on,
    tool_whats_central,
    tool_whats_in,
    tool_who_calls,
)


@pytest.fixture
def daemon_running(repo: Path):
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
        pytest.fail("daemon failed to come up")
    try:
        yield repo
    finally:
        server.stop()
        thread.join(timeout=2.0)


def test_intent_tool_via_daemon(daemon_running: Path) -> None:
    resp = tool_whats_in({"repo": str(daemon_running), "path": "auth.py"})
    assert resp["ok"] is True
    assert resp["results"]
    assert "freshness" in resp
    assert "receipt" in resp


def test_intent_tool_direct_fallback(repo: Path) -> None:
    """When the daemon isn't running, the wrapper runs the handler in-process."""
    resp = tool_who_calls({"repo": str(repo), "node": "AuthService.login"})
    assert resp["ok"] is True
    assert any("auth.helper" in (r.get("label") or "") for r in resp["results"])
    assert "freshness" in resp
    assert resp["receipt"]["op"] == "who_calls"


def test_find_by_concept_via_direct_path(repo: Path) -> None:
    resp = tool_find_by_concept({"repo": str(repo), "query": "authenticate user login"})
    assert resp["ok"] is True
    # tiny graphs may yield zero hits; what matters is shape.
    assert "results" in resp


def test_what_depends_on_via_direct(repo: Path) -> None:
    resp = tool_what_depends_on({"repo": str(repo), "node": "AuthService.validate"})
    assert resp["ok"] is True
    labels = [r["label"] for r in resp["results"]]
    assert any("AuthService" in lab for lab in labels)


def test_find_by_name_via_direct(repo: Path) -> None:
    resp = tool_find_by_name({"repo": str(repo), "label": "auth"})
    assert resp["ok"] is True
    assert any("AuthService" in (r.get("label") or "") for r in resp["results"])


def test_whats_central_via_direct(repo: Path) -> None:
    resp = tool_whats_central({"repo": str(repo), "top_k": 3})
    assert resp["ok"] is True
    assert len(resp["results"]) <= 3


def test_intent_tool_error_propagates(repo: Path) -> None:
    resp = tool_who_calls({"repo": str(repo), "node": "definitely_not_a_symbol"})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "NOT_FOUND"
