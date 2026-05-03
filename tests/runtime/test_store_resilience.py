"""Phase 11.3 cache resilience tests."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from graphify_plus.interface.errors import (
    CacheCorrupt,
    CacheLocked,
    CacheVersionMismatch,
)
from graphify_plus.runtime.store import SCHEMA_VERSION, Store, cache_path


def _open(tmp_path: Path) -> Store:
    return Store(cache_path(tmp_path))


def test_schema_version_written_on_first_open(tmp_path: Path):
    s = _open(tmp_path)
    try:
        assert s.get_meta("schema_version") == str(SCHEMA_VERSION)
    finally:
        s.close()


def test_corrupt_file_is_quarantined(tmp_path: Path):
    db = cache_path(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    # Write garbage so SQLite can't even open it.
    db.write_bytes(b"\x00not-a-database-file" * 100)
    with pytest.raises(CacheCorrupt):
        _open(tmp_path)
    # Original file moved aside, no longer exists at cache.db.
    assert not db.exists()
    siblings = list(db.parent.glob("cache.db.corrupt.*"))
    assert siblings, "expected quarantined backup"


def test_quarantine_helper_renames_to_corrupt_sibling(tmp_path: Path):
    """Smoke the _quarantine codepath directly — sibling 'corrupt' file
    is created with the expected timestamp pattern."""
    s = _open(tmp_path)
    assert s.db_path.exists()
    s._quarantine(reason="test")
    assert not s.db_path.exists()
    sibs = sorted(s.db_path.parent.glob("cache.db.corrupt.*"))
    assert sibs
    # Timestamp suffix shape: YYYYMMDDTHHMMSSZ.
    name = sibs[-1].name
    import re

    assert re.match(r"^cache\.db\.corrupt\.\d{8}T\d{6}Z$", name), name


def test_newer_on_disk_schema_raises_version_mismatch(tmp_path: Path):
    s = _open(tmp_path)
    try:
        s.set_meta("schema_version", str(SCHEMA_VERSION + 1))
    finally:
        s.close()
    with pytest.raises(CacheVersionMismatch):
        _open(tmp_path)


def test_older_on_disk_schema_raises_version_mismatch(tmp_path: Path):
    if SCHEMA_VERSION <= 0:
        pytest.skip("schema version 0 has no 'older' case")
    s = _open(tmp_path)
    try:
        s.set_meta("schema_version", str(SCHEMA_VERSION - 1))
    finally:
        s.close()
    with pytest.raises(CacheVersionMismatch):
        _open(tmp_path)


def test_backup_creates_sibling_file(tmp_path: Path):
    s = _open(tmp_path)
    try:
        s.set_meta("seed", "value")
        bak = s.backup()
    finally:
        s.close()
    assert bak is not None
    assert bak.name == "cache.db.bak"
    assert bak.exists()


def test_concurrent_write_does_not_deadlock(tmp_path: Path):
    """Two writers — second one must either succeed eventually or raise
    CacheLocked. Never deadlocks.
    """
    # Pre-create the DB so both threads attach to an existing file rather
    # than racing to bootstrap the schema.
    bootstrap = _open(tmp_path)
    bootstrap.close()

    barrier = threading.Barrier(2)
    results: list[Exception | None] = []

    def writer():
        s = _open(tmp_path)
        try:
            barrier.wait()
            with s.tx():
                s.conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                    (f"k_{threading.get_ident()}", "v"),
                )
            results.append(None)
        except Exception as e:  # noqa: BLE001
            results.append(e)
        finally:
            s.close()

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    # Both threads must have finished.
    assert all(not t.is_alive() for t in threads)
    # At least one must have succeeded; the other either succeeded or
    # raised CacheLocked. Never something else.
    assert any(r is None for r in results)
    for r in results:
        if r is not None:
            assert isinstance(r, CacheLocked)


def test_tx_rolls_back_on_exception(tmp_path: Path):
    s = _open(tmp_path)
    try:
        s.set_meta("seed", "before")
        with pytest.raises(RuntimeError):
            with s.tx():
                s.conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                    ("seed", "during"),
                )
                raise RuntimeError("boom")
        # Roll-back: 'seed' still equals 'before'.
        assert s.get_meta("seed") == "before"
    finally:
        s.close()
