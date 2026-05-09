"""Tests for Layer 14 (provenance + hallucination filter) and 18 (embeddings)."""

from __future__ import annotations

from pathlib import Path

from graphify_plus.daemon.embeddings import (
    EmbeddingState,
    rerank_or_passthrough,
    reset_state_for_tests,
    state,
)
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.provenance import (
    ALLOWED_RELATIONS,
    Provenance,
    evaluate_negative_sampling,
    filter_llm_edges,
    hash_prompt,
    load_provenance,
    store_provenance,
)
from graphify_plus.runtime.store import Store, cache_path

# ---- Layer 14 ------------------------------------------------------------


def test_provenance_ast_constructor() -> None:
    p = Provenance.ast("treesitter-python@0.21.0", source_sha="abc")
    assert p.extractor_kind == "AST"
    assert p.confidence == 1.0
    assert p.source_sha == "abc"


def test_provenance_llm_constructor() -> None:
    p = Provenance.llm("gpt-4", prompt_hash="h1", confidence=0.7)
    assert p.extractor_kind == "LLM"
    assert p.confidence == 0.7
    assert p.model == "gpt-4"


def test_filter_llm_edges_drops_ungrounded() -> None:
    symbols = [{"id": "a"}, {"id": "b"}]
    edges = [
        {"src": "a", "dst": "b", "kind": "calls"},
        {"src": "a", "dst": "c", "kind": "calls"},  # c isn't a known symbol
    ]
    out, stats = filter_llm_edges(edges, symbols)
    assert len(out) == 1
    assert stats.after_grounding == 1
    assert stats.rejected[0]["reason"] == "ungrounded"


def test_filter_llm_edges_drops_unknown_relation() -> None:
    symbols = [{"id": "a"}, {"id": "b"}]
    edges = [
        {"src": "a", "dst": "b", "kind": "weird-relation"},
        {"src": "a", "dst": "b", "kind": "calls"},
    ]
    out, stats = filter_llm_edges(edges, symbols)
    assert len(out) == 1
    assert all(e["kind"] in ALLOWED_RELATIONS for e in out)


def test_filter_llm_edges_anchor_rejects_missing_snippet() -> None:
    symbols = [{"id": "a"}, {"id": "b"}]
    edges = [
        {
            "src": "a",
            "dst": "b",
            "kind": "calls",
            "evidence_snippet": "real text",
            "evidence_path": "auth.py",
        },
        {
            "src": "a",
            "dst": "b",
            "kind": "calls",
            "evidence_snippet": "fabricated text",
            "evidence_path": "auth.py",
        },
    ]
    snippets = {"auth.py": "real text in the file"}
    out, stats = filter_llm_edges(edges, symbols, snippet_index=snippets)
    assert len(out) == 1
    assert any(r["reason"] == "unanchored" for r in stats.rejected)


def test_evaluate_negative_sampling() -> None:
    out = evaluate_negative_sampling(
        real_edges=[{"src": "a", "dst": "b"}] * 10,
        decoy_edges=[{"src": "x", "dst": "y"}] * 3,
    )
    assert out["balance_ok"] is True


def test_hash_prompt_deterministic() -> None:
    assert hash_prompt("hello") == hash_prompt("hello")
    assert hash_prompt("hello") != hash_prompt("world")


def test_provenance_store_load_roundtrip(repo: Path) -> None:
    store = Store(cache_path(repo))
    try:
        store_provenance(store, {"sym1": Provenance.ast("ts@0.21")})
        loaded = load_provenance(store)
    finally:
        store.close()
    assert "sym1" in loaded
    assert loaded["sym1"]["extractor_kind"] == "AST"


# ---- Layer 18 ------------------------------------------------------------


def test_state_unavailable_when_dep_missing(snapshot: InMemoryGraph) -> None:
    """When sentence-transformers isn't installed, state() reports the
    unavailability gracefully."""
    reset_state_for_tests(EmbeddingState(available=False, model_name="x", reason="test"))
    s = state()
    assert s.available is False


def test_rerank_passthrough_when_unavailable(snapshot: InMemoryGraph) -> None:
    reset_state_for_tests(EmbeddingState(available=False, model_name="x"))
    candidates = [{"node_id": "a", "label": "a"}, {"node_id": "b", "label": "b"}]
    out = rerank_or_passthrough(snapshot, "test", candidates)
    assert out == candidates  # unchanged
    reset_state_for_tests(None)


def test_search_returns_none_when_unavailable(snapshot: InMemoryGraph) -> None:
    from graphify_plus.daemon.embeddings import search

    reset_state_for_tests(EmbeddingState(available=False, model_name="x"))
    assert search(snapshot, "anything") is None
    reset_state_for_tests(None)
