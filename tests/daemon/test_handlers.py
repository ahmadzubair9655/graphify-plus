"""Tests for the intent-typed tool handlers."""

from __future__ import annotations

from graphify_plus.daemon.handlers import (
    DEFAULT_BUDGET_TOKENS,
    HANDLERS,
    find_by_concept,
    find_by_name,
    what_depends_on,
    whats_central,
    whats_in,
    who_calls,
)
from graphify_plus.daemon.indexes import InMemoryGraph


def _has(rows: list[dict], label: str) -> bool:
    return any(label in (row.get("label") or "") for row in rows)


def _row_shape_ok(row: dict) -> bool:
    required = {"node_id", "label", "source_file", "line_number", "snippet", "confidence", "kind"}
    return required.issubset(row.keys())


def test_handler_registry_is_complete() -> None:
    expected = {
        "whats_in",
        "who_calls",
        "whos_called_by",
        "what_depends_on",
        "what_does_this_depend_on",
        "find_by_name",
        "find_by_concept",
        "whats_central",
        "graph_stats",
    }
    assert expected.issubset(HANDLERS.keys())


def test_whats_in_returns_file_line_grounded_rows(snapshot: InMemoryGraph) -> None:
    resp = whats_in(snapshot, {"path": "auth.py"})
    assert resp["results"]
    for row in resp["results"]:
        assert _row_shape_ok(row), row
        assert row["source_file"] == "auth.py"
        assert row["line_number"] >= 1


def test_whats_in_directory_prefix(snapshot: InMemoryGraph) -> None:
    resp = whats_in(snapshot, {"path": "."})
    assert resp["results"]


def test_whats_in_unknown_path(snapshot: InMemoryGraph) -> None:
    resp = whats_in(snapshot, {"path": "no_such_file.py"})
    assert resp["results"] == []


def test_who_calls_resolves_via_short_name(snapshot: InMemoryGraph) -> None:
    resp = who_calls(snapshot, {"node": "AuthService.login"})
    assert _has(resp["results"], "auth.helper")
    assert "target" in resp.get("extra", {})


def test_who_calls_returns_ambiguous_for_overloaded_name(snapshot: InMemoryGraph) -> None:
    # Inject a synthetic ambiguity by adding two symbols with the same
    # ``name`` to the snapshot's name index.
    fake = {
        "id": "fake",
        "name": "login",
        "qualified_name": "other.Login.login",
        "kind": "method",
        "path": "other.py",
        "span": (1, 1),
    }
    snapshot.by_id["fake"] = fake
    snapshot.by_name.setdefault("login", []).append("fake")
    resp = who_calls(snapshot, {"node": "login"})
    # Either resolved by the existing exact-qname rule (one wins) or AMBIGUOUS
    # — both are acceptable; the contract is that the caller never gets a
    # silently-wrong answer when their name is non-unique.
    assert resp.get("error", {}).get("code") == "AMBIGUOUS" or _has(resp["results"], "auth.helper")


def test_who_calls_not_found(snapshot: InMemoryGraph) -> None:
    resp = who_calls(snapshot, {"node": "definitely_not_a_symbol"})
    assert resp.get("error", {}).get("code") == "NOT_FOUND"


def test_who_calls_missing_args(snapshot: InMemoryGraph) -> None:
    resp = who_calls(snapshot, {})
    assert resp.get("error", {}).get("code") == "BAD_REQUEST"


def test_what_depends_on_uses_inbound_edges(snapshot: InMemoryGraph) -> None:
    resp = what_depends_on(snapshot, {"node": "AuthService.validate"})
    labels = [r["label"] for r in resp["results"]]
    assert any("AuthService" in lab for lab in labels)


def test_find_by_name_confidence_ladder(snapshot: InMemoryGraph) -> None:
    # exact qname → 1.0
    exact = find_by_name(snapshot, {"label": "auth.AuthService"})
    assert exact["results"]
    assert exact["results"][0]["confidence"] == 1.0
    # ``auth`` is exact for the module but only a prefix for AuthService —
    # at least one result should land below 1.0 (the prefix one).
    prefix = find_by_name(snapshot, {"label": "auth"})
    assert len(prefix["results"]) >= 2
    assert any(r["confidence"] < 1.0 for r in prefix["results"])


def test_find_by_concept_returns_normalised_scores(snapshot: InMemoryGraph) -> None:
    resp = find_by_concept(snapshot, {"query": "authenticate user login"})
    assert resp["results"]
    top = resp["results"][0]
    assert 0.0 < top["confidence"] <= 1.0
    assert _row_shape_ok(top)


def test_find_by_concept_heavy_path_returns_results(snapshot: InMemoryGraph) -> None:
    resp = find_by_concept(snapshot, {"query": "authenticate user login", "heavy": True})
    # heavy path may legitimately return zero rows on tiny graphs (no
    # community structure), but should never crash and the shape must be
    # consistent.
    assert "results" in resp
    for row in resp["results"]:
        assert _row_shape_ok(row)


def test_token_budget_clipping(snapshot: InMemoryGraph) -> None:
    # An absurdly low budget forces clipping; first row is always kept.
    resp = whats_in(snapshot, {"path": "auth.py", "budget_tokens": 1})
    assert len(resp["results"]) == 1
    assert resp["more_available"] >= 1


def test_default_budget_is_applied(snapshot: InMemoryGraph) -> None:
    resp = whats_in(snapshot, {"path": "auth.py"})
    # The total payload must sit comfortably below the default budget.
    from graphify_plus.daemon.receipts import estimate_tokens

    assert estimate_tokens(resp["results"]) <= DEFAULT_BUDGET_TOKENS + 256


def test_whats_central_respects_top_k(snapshot: InMemoryGraph) -> None:
    resp = whats_central(snapshot, {"top_k": 3})
    assert len(resp["results"]) <= 3
    for row in resp["results"]:
        assert "pagerank" in row


def test_node_id_passes_through(snapshot: InMemoryGraph) -> None:
    sid = next(
        s["id"]
        for s in snapshot.by_id.values()
        if s.get("qualified_name") == "auth.AuthService.login"
    )
    resp = who_calls(snapshot, {"node_id": sid})
    assert resp.get("extra", {}).get("target") == sid
