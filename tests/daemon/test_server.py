"""Tests for the Unix-socket RPC server.

Each test starts a real ``DaemonServer`` in a thread (see conftest's
``running_daemon`` fixture) and talks to it via the public ``DaemonClient``.
This catches protocol / serialization bugs that pure-handler tests miss.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from graphify_plus.daemon import DaemonClient
from graphify_plus.daemon.client import DaemonError


def test_ping_round_trip(running_daemon: DaemonClient) -> None:
    resp = running_daemon.call("ping")
    assert resp["ok"] is True
    assert resp.get("extra", {}).get("pong") is True


def test_query_returns_envelope(running_daemon: DaemonClient) -> None:
    resp = running_daemon.call("whats_in", {"path": "auth.py"})
    assert resp["ok"] is True
    assert "freshness" in resp
    assert "receipt" in resp
    assert isinstance(resp["results"], list)
    assert resp["results"]
    assert resp["receipt"]["op"] == "whats_in"
    assert resp["receipt"]["elapsed_ms"] >= 0


def test_query_latency_is_fast(running_daemon: DaemonClient) -> None:
    """Sprint 1 win condition: warm-cache queries return in well under 100ms.

    The CI threshold is generous (200ms) — tightening this is a CI policy
    change. The daemon's *internal* elapsed_ms should be <20ms.
    """
    # Warm up once.
    running_daemon.call("whats_in", {"path": "auth.py"})
    t0 = time.perf_counter()
    resp = running_daemon.call("whats_in", {"path": "auth.py"})
    wall_ms = (time.perf_counter() - t0) * 1000.0
    assert wall_ms < 200.0, f"wall-clock too slow: {wall_ms:.1f}ms"
    assert resp["receipt"]["elapsed_ms"] < 20.0


def test_unknown_op_returns_error(running_daemon: DaemonClient) -> None:
    with pytest.raises(DaemonError) as exc:
        running_daemon.call("absolutely_not_a_real_op")
    assert exc.value.code == "UNKNOWN_OP"


def test_handler_error_propagates(running_daemon: DaemonClient) -> None:
    with pytest.raises(DaemonError) as exc:
        running_daemon.call("who_calls", {"node": "nope"})
    assert exc.value.code == "NOT_FOUND"


def test_refresh_is_idempotent(running_daemon: DaemonClient) -> None:
    first = running_daemon.call("refresh")
    second = running_daemon.call("refresh")
    assert first["ok"] and second["ok"]
    assert "freshness_token" in first.get("extra", {})


def test_freshness_degrades_after_touch(running_daemon: DaemonClient, repo: Path) -> None:
    # Kick mtime past the snapshot's threshold.
    auth = repo / "auth.py"
    bumped = auth.stat().st_mtime + 5.0
    os.utime(auth, (bumped, bumped))
    resp = running_daemon.call("whats_in", {"path": "auth.py"})
    assert resp["freshness"]["trust"] == "STALE_FILES"
    assert "auth.py" in resp["freshness"]["stale_paths"]
    # After refresh, freshness comes back.
    running_daemon.call("refresh")
    resp = running_daemon.call("whats_in", {"path": "auth.py"})
    assert resp["freshness"]["trust"] == "FRESH"


def test_client_recovers_after_reconnect(running_daemon: DaemonClient) -> None:
    # The client opens a fresh connection per call — make sure two
    # back-to-back calls both succeed.
    a = running_daemon.call("graph_stats")
    b = running_daemon.call("graph_stats")
    assert a["ok"] and b["ok"]
    assert a["extra"]["symbols"] == b["extra"]["symbols"]


def test_is_running_reports_true(running_daemon: DaemonClient) -> None:
    assert running_daemon.is_running() is True
