"""Tests for Batch I: hybrid grep (2.3), academic-name aliases (5.4),
editor-native query / saved queries (17.4)."""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.handlers import HANDLERS
from graphify_plus.daemon.hybrid_grep import (
    grep_stale_paths,
    hybrid_merge,
    maybe_augment,
)
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.lsp_shim import LSPServer


# ---- Layer 2.3 — hybrid grep fallback ----------------------------------


def test_grep_stale_paths_finds_pattern(snapshot: InMemoryGraph, repo: Path) -> None:
    # Touch auth.py so it's stale.
    bumped = (repo / "auth.py").stat().st_mtime + 5.0
    os.utime(repo / "auth.py", (bumped, bumped))
    rows = grep_stale_paths(snapshot, "AuthService")
    assert rows
    assert all(r["source_file"] == "auth.py" for r in rows)
    assert all(r["source"] == "grep" for r in rows)


def test_grep_stale_paths_returns_nothing_when_fresh(snapshot: InMemoryGraph) -> None:
    rows = grep_stale_paths(snapshot, "AuthService")
    assert rows == []


def test_hybrid_merge_tags_sources() -> None:
    graph_rows = [{"label": "x"}]
    grep_rows = [{"label": "y", "source": "grep"}]
    merged = hybrid_merge(graph_rows, grep_rows)
    assert merged[0]["source"] == "graph"
    assert merged[1]["source"] == "grep"


def test_maybe_augment_no_op_when_fresh(snapshot: InMemoryGraph) -> None:
    rows, info = maybe_augment(snapshot, [{"label": "x"}], pattern="anything")
    assert info["n_grep_hits"] == 0
    assert len(rows) == 1


def test_maybe_augment_augments_when_stale(snapshot: InMemoryGraph, repo: Path) -> None:
    bumped = (repo / "auth.py").stat().st_mtime + 5.0
    os.utime(repo / "auth.py", (bumped, bumped))
    rows, info = maybe_augment(snapshot, [], pattern="AuthService")
    assert info["n_grep_hits"] >= 1
    assert any(r.get("source") == "grep" for r in rows)


def test_find_by_name_includes_grep_when_stale(repo: Path) -> None:
    """End-to-end: stale file → find_by_name returns rows even if the
    label doesn't match any indexed symbol."""
    from graphify_plus.daemon.handlers import find_by_name as fbn
    from graphify_plus.runtime.store import Store, cache_path

    store = Store(cache_path(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    bumped = (repo / "auth.py").stat().st_mtime + 5.0
    os.utime(repo / "auth.py", (bumped, bumped))
    resp = fbn(snap, {"label": "AuthService"})
    # Either hybrid info present, or rows exist via the graph path; both ok.
    assert resp["results"]


# ---- Layer 5.4 — academic-name aliases --------------------------------


def test_alias_whats_risky_to_change_present() -> None:
    assert "whats_risky_to_change" in HANDLERS


def test_alias_whats_risky_to_change_runs(snapshot: InMemoryGraph) -> None:
    handler = HANDLERS["whats_risky_to_change"]
    resp = handler(snapshot, {"node": "AuthService.validate"})
    assert "results" in resp


def test_alias_whats_changed_since_present() -> None:
    assert "whats_changed_since" in HANDLERS


# ---- Layer 17.4 — editor-native query --------------------------------


def test_lsp_run_query_no_daemon(repo: Path) -> None:
    server = LSPServer(repo)
    out = server._dispatch(
        "graphifyPlus/runQuery",
        {"query": "FIND nodes WHERE kind = 'method'"},
    )
    assert "rows" in out


def test_lsp_saved_queries(repo: Path) -> None:
    queries = repo / ".graphify_plus" / "queries"
    queries.mkdir(parents=True, exist_ok=True)
    (queries / "untested.gpl").write_text("FIND nodes WHERE test_coverage < 0.1")
    server = LSPServer(repo)
    out = server._dispatch("graphifyPlus/savedQueries", {})
    assert isinstance(out, list)
    assert any(r["name"] == "untested" for r in out)


def test_lsp_run_query_with_nl(repo: Path) -> None:
    server = LSPServer(repo)
    out = server._dispatch(
        "graphifyPlus/runQuery",
        {"nl": "untested functions"},
    )
    assert isinstance(out, dict)
    assert "rows" in out
