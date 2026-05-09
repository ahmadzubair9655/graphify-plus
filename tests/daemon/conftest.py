"""Shared fixtures for the daemon tests.

The fixture seeds a tiny Python repo, runs ``ingest``, and yields the
``Path`` to the workspace. Tests that need an ``InMemoryGraph`` build it
from the on-disk store; tests that need a running daemon spin up a
``DaemonServer`` thread bound to a per-test socket.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from graphify_plus.core.ingest import ingest
from graphify_plus.daemon import DaemonClient, DaemonServer
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.runtime.store import Store, cache_path


SAMPLE_PY = """\
\"\"\"sample auth module.\"\"\"


class AuthService:
    \"\"\"Authenticates users.\"\"\"

    def login(self, user, password):
        return self.validate(user, password)

    def validate(self, user, password):
        return True


def helper():
    s = AuthService()
    return s.login('a', 'b')
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tmp_path repo seeded with a Python file and a fresh graph cache."""
    (tmp_path / "auth.py").write_text(SAMPLE_PY)
    res = ingest(tmp_path, parallel=False)
    store = Store(cache_path(tmp_path))
    try:
        store.replace_all(res.symbols, res.edges)
    finally:
        store.close()
    return tmp_path


@pytest.fixture
def snapshot(repo: Path) -> InMemoryGraph:
    store = Store(cache_path(repo))
    try:
        return InMemoryGraph.from_store(store, repo)
    finally:
        store.close()


@pytest.fixture
def running_daemon(repo: Path) -> Iterator[DaemonClient]:
    """Start a ``DaemonServer`` in a background thread bound to the repo's
    per-workspace socket. Yields a ``DaemonClient`` ready to call.
    """
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
        yield client
    finally:
        server.stop()
        thread.join(timeout=2.0)
