"""Tests for the daemon's pre-computed indexes."""

from __future__ import annotations

from pathlib import Path

from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.runtime.store import Store, cache_path


def test_indexes_populate(snapshot: InMemoryGraph) -> None:
    assert len(snapshot.by_id) >= 4
    assert "auth.py" in snapshot.by_path
    # Path-keyed list is ordered by line number.
    line_numbers = [
        (snapshot.by_id[s].get("span") or (0, 0))[0] for s in snapshot.by_path["auth.py"]
    ]
    assert line_numbers == sorted(line_numbers)


def test_pagerank_top_excludes_external_placeholders(snapshot: InMemoryGraph) -> None:
    for sid, _score in snapshot.pagerank_top:
        sym = snapshot.by_id[sid]
        assert sym.get("kind") != "external"


def test_freshness_token_is_stable_for_same_inputs(snapshot: InMemoryGraph, repo: Path) -> None:
    store = Store(cache_path(repo))
    try:
        again = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    # freshness_token mixes built_at, so it changes per-build, but the
    # symbol/edge counts must be identical.
    assert len(again.by_id) == len(snapshot.by_id)
    assert sum(len(v) for v in again.out_neighbours.values()) == sum(
        len(v) for v in snapshot.out_neighbours.values()
    )


def test_find_by_name_exact_qname(snapshot: InMemoryGraph) -> None:
    qname = "auth.AuthService"
    matches = snapshot.find_by_name(qname)
    assert matches and matches[0].get("qualified_name") == qname


def test_find_by_name_short_name_prefix(snapshot: InMemoryGraph) -> None:
    matches = snapshot.find_by_name("auth")
    labels = [m.get("qualified_name") for m in matches]
    assert any(label and "AuthService" in label for label in labels), labels


def test_find_by_name_suffix_match(snapshot: InMemoryGraph) -> None:
    matches = snapshot.find_by_name("AuthService.login")
    assert matches
    assert matches[0].get("qualified_name") == "auth.AuthService.login"


def test_text_search_returns_relevant_hits(snapshot: InMemoryGraph) -> None:
    ranked = snapshot.text_search("authenticate user")
    assert ranked
    top = ranked[0][0]
    assert "auth" in (top.get("qualified_name") or "").lower()


def test_callers_via_placeholder_alias(snapshot: InMemoryGraph) -> None:
    """``s.login`` and ``self.login`` are placeholder targets — the daemon
    should still surface real callers when asked who calls AuthService.login.
    """
    login = next(
        sym
        for sym in snapshot.by_id.values()
        if sym.get("qualified_name") == "auth.AuthService.login"
    )
    callers = snapshot.callers_of(login["id"])
    qnames = {c.get("qualified_name") for c in callers}
    assert "auth.helper" in qnames, f"expected helper among callers, got {qnames}"


def test_dependents_includes_class_via_contains(snapshot: InMemoryGraph) -> None:
    validate = next(
        sym
        for sym in snapshot.by_id.values()
        if sym.get("qualified_name") == "auth.AuthService.validate"
    )
    qnames = {s.get("qualified_name") for s in snapshot.dependents_of(validate["id"])}
    assert "auth.AuthService" in qnames  # class -contains-> validate
    # And via placeholder, login depends on validate.
    assert "auth.AuthService.login" in qnames


def test_freshness_envelope_fresh(snapshot: InMemoryGraph) -> None:
    fresh = snapshot.freshness()
    assert fresh["trust"] == "FRESH"
    assert fresh["files_changed_since"] == 0
    assert fresh["freshness_token"]


def test_freshness_envelope_stale_after_touch(snapshot: InMemoryGraph, repo: Path) -> None:
    # Stat-cache invalidation: bump the mtime by 5s to be safely past
    # the daemon's ±1µs tolerance and any filesystem coarseness.
    import os

    auth = repo / "auth.py"
    bumped = auth.stat().st_mtime + 5.0
    os.utime(auth, (bumped, bumped))
    fresh = snapshot.freshness()
    assert fresh["trust"] == "STALE_FILES"
    assert fresh["files_changed_since"] >= 1
    assert "auth.py" in fresh["stale_paths"]
    assert "auth.py" in fresh["hint"]
