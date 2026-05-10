"""Tests for per-call receipts."""

from __future__ import annotations

from graphify_plus.daemon.receipts import (
    estimate_tokens,
    format_receipt_line,
    grep_equivalent_for,
    make_receipt,
)


def test_estimate_tokens_handles_dicts() -> None:
    payload = {"a": "x" * 40, "b": [1, 2, 3]}
    n = estimate_tokens(payload)
    assert n >= 10


def test_estimate_tokens_minimum_one() -> None:
    assert estimate_tokens(None) >= 1
    assert estimate_tokens({}) >= 1


def test_grep_equivalent_zero_results() -> None:
    msg = grep_equivalent_for("who_calls", 0)
    assert "zero round-trips" in msg


def test_grep_equivalent_per_op_strings() -> None:
    for op in ("whats_in", "who_calls", "what_depends_on", "find_by_name", "find_by_concept"):
        out = grep_equivalent_for(op, 5)
        assert isinstance(out, str) and out


def test_make_receipt_shape() -> None:
    r = make_receipt("who_calls", 12.5, [{"x": 1}], 1)
    assert r["op"] == "who_calls"
    assert r["elapsed_ms"] == 12.5
    assert r["tokens"] >= 1
    assert "grep_equivalent" in r


def test_format_receipt_line_contains_signature() -> None:
    r = make_receipt("whats_in", 4.0, [{"x": 1}], 3)
    line = format_receipt_line(r)
    assert line.startswith("[graphify-plus]")
    assert "whats_in" in line
    assert "4.0ms" in line
