"""12.2 — repo-level lock with PID liveness check."""

from __future__ import annotations

import json
import os

import pytest

from graphify_plus.runtime.repolock import RepoLock, RepoLocked, _lock_path


def test_acquire_and_release(tmp_path):
    with RepoLock(tmp_path) as _lock:
        assert _lock_path(tmp_path).exists()
    assert not _lock_path(tmp_path).exists()


def test_re_entrant_blocked_by_self(tmp_path):
    with RepoLock(tmp_path):
        with pytest.raises(RepoLocked):
            with RepoLock(tmp_path):
                pass


def test_stale_lock_is_reclaimed(tmp_path):
    p = _lock_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Use a PID that cannot be alive (very large, very unlikely).
    p.write_text(json.dumps({"pid": 2**31 - 1, "ts": "1970-01-01T00:00:00+00:00"}))
    with RepoLock(tmp_path):
        holder = json.loads(p.read_text())
        assert holder["pid"] == os.getpid()


def test_live_holder_blocks(tmp_path):
    p = _lock_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pid": os.getpid(), "ts": "x"}))
    # Same PID is "alive" per os.kill(pid, 0).
    with pytest.raises(RepoLocked):
        with RepoLock(tmp_path):
            pass
    p.unlink()
