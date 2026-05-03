from __future__ import annotations

import time
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from graphify_plus.runtime.session import Session, prune_expired
from graphify_plus.runtime.store import Store, cache_path


def _store(tmp_path: Path) -> Store:
    return Store(cache_path(tmp_path))


def test_session_records_and_diffs(tmp_path: Path):
    s = _store(tmp_path)
    try:
        sess = Session.start(s)
        seen, new = sess.delta(["aaa", "bbb"])
        assert seen == set()
        assert new == {"aaa", "bbb"}
        sess.touch_many(new)
        seen, new = sess.delta(["aaa", "bbb"])
        assert seen == {"aaa", "bbb"}
        assert new == set()
    finally:
        s.close()


def test_session_distinct_ids(tmp_path: Path):
    s = _store(tmp_path)
    try:
        a = Session.start(s)
        b = Session.start(s)
        assert a.id != b.id
        a.touch("h1")
        seen_a, _ = a.delta(["h1"])
        seen_b, _ = b.delta(["h1"])
        assert seen_a == {"h1"}
        assert seen_b == set()
    finally:
        s.close()


def test_session_prune(tmp_path: Path):
    s = _store(tmp_path)
    try:
        sess = Session.start(s)
        sess.touch_many(["x", "y", "z"])
        # Force-age the rows by setting created_at to 0.
        s.conn.execute("UPDATE sessions SET created_at = 0 WHERE id = ?", (sess.id,))
        n = prune_expired(s, ttl_sec=1)
        assert n >= 4  # 3 hashes + sentinel
    finally:
        s.close()


@given(hashes=st.lists(st.text(min_size=1, max_size=8), min_size=1, max_size=20))
def test_property_full_resend_yields_empty_delta(hashes, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp(f"sess_{int(time.time() * 1e6)}")
    s = _store(tmp_path)
    try:
        sess = Session.start(s)
        sess.touch_many(hashes)
        seen, new = sess.delta(hashes)
        assert new == set()
        assert seen == set(hashes)
    finally:
        s.close()
