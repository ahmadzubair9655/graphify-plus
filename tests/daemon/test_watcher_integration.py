"""Watcher-driven incremental refresh — Sprint 3 of the master plan.

The daemon owns a watcher that re-runs ingest on changed files (already
implemented in ``runtime/watcher.py``) and, on each tick, signals the
daemon to swap in a fresh ``InMemoryGraph`` snapshot. This test edits a
file and verifies the daemon picks up the new symbol without us calling
``refresh`` ourselves.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from graphify_plus.daemon import DaemonClient, DaemonServer


@pytest.fixture
def daemon_with_watcher(repo: Path):
    server = DaemonServer(repo, watch=True)
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
        pytest.fail("daemon did not become ready")
    try:
        yield server, client
    finally:
        server.stop()
        thread.join(timeout=3.0)


def _wait_for_token_change(client: DaemonClient, prev: str, timeout: float = 8.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.call("graph_stats")
        cur = resp.get("freshness", {}).get("freshness_token", "")
        if cur and cur != prev:
            return cur
        time.sleep(0.1)
    return prev


def test_daemon_picks_up_new_symbol_on_file_save(
    daemon_with_watcher: tuple[DaemonServer, DaemonClient], repo: Path
) -> None:
    _server, client = daemon_with_watcher
    initial = client.call("graph_stats")
    initial_token = initial["freshness"]["freshness_token"]
    initial_symbols = initial["extra"]["symbols"]

    # Edit a file: introduce a new top-level function.
    new_src = (repo / "auth.py").read_text() + "\n\ndef brand_new():\n    return 42\n"
    (repo / "auth.py").write_text(new_src)
    # Force the mtime forward enough for any low-resolution filesystems.
    bumped = (repo / "auth.py").stat().st_mtime + 5.0
    os.utime(repo / "auth.py", (bumped, bumped))

    new_token = _wait_for_token_change(client, initial_token, timeout=8.0)
    assert new_token != initial_token, "daemon never refreshed after file save"

    after = client.call("graph_stats")
    assert after["extra"]["symbols"] == initial_symbols + 1, (
        f"expected {initial_symbols + 1} symbols, got {after['extra']['symbols']}"
    )
    # And the new symbol is queryable.
    found = client.call("find_by_name", {"label": "brand_new"})
    labels = [r["label"] for r in found["results"]]
    assert any("brand_new" in lab for lab in labels), labels


def test_no_watch_flag_disables_auto_refresh(repo: Path) -> None:
    """With ``watch=False`` the daemon must NOT pick up file changes on
    its own. ``refresh`` only reloads from on-disk SQLite; updating that
    store is an explicit step (``gp init`` or ``gp watch``) when the
    embedded watcher is off.
    """
    from graphify_plus.core.ingest import ingest
    from graphify_plus.runtime.store import Store, cache_path

    server = DaemonServer(repo, watch=False)
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
        pytest.fail("daemon did not become ready")

    try:
        before = client.call("graph_stats")
        before_symbols = before["extra"]["symbols"]
        (repo / "auth.py").write_text(
            (repo / "auth.py").read_text() + "\n\ndef untracked():\n    return 1\n"
        )
        bumped = (repo / "auth.py").stat().st_mtime + 5.0
        os.utime(repo / "auth.py", (bumped, bumped))
        time.sleep(0.5)
        mid = client.call("graph_stats")
        assert mid["extra"]["symbols"] == before_symbols
        assert mid["freshness"]["trust"] == "STALE_FILES"
        # User runs the equivalent of `gp init` to update SQLite, then refresh.
        res = ingest(repo, parallel=False)
        store = Store(cache_path(repo))
        try:
            store.replace_all(res.symbols, res.edges)
        finally:
            store.close()
        client.call("refresh")
        after = client.call("graph_stats")
        assert after["extra"]["symbols"] == before_symbols + 1
        assert after["freshness"]["trust"] == "FRESH"
    finally:
        server.stop()
        thread.join(timeout=3.0)
