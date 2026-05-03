"""Bug 6 (v5.0.3): skipped probes show up under ``skipped_components``
with grade=SKIP and a reason, instead of silently dropping out of the
report.
"""

from __future__ import annotations

import networkx as nx

from graphify_plus.audit.probe import (
    _aggregate_grade,
    format_audit_report,
    run_audit,
)


def test_aggregate_records_skipped_components():
    results = {
        "edge_deletion_stability": {"skipped": True, "reason": "no community attribute on nodes"},
        "rename_sensitivity": {"rename_grade": "A"},
    }
    out = _aggregate_grade(results)
    assert "edge_deletion_stability" in out["skipped_components"]
    skip = out["skipped_components"]["edge_deletion_stability"]
    assert skip["grade"] == "SKIP"
    assert "community" in skip["reason"].lower()
    # Skipped probes do not contribute to weighted score.
    assert "edge_deletion_stability" not in out["components"]
    assert "rename_sensitivity" in out["components"]


def test_aggregate_handles_skip_with_no_reason():
    results = {"confidence_drift": {"skipped": True}}
    out = _aggregate_grade(results)
    info = out["skipped_components"]["confidence_drift"]
    assert info["grade"] == "SKIP"
    assert info["reason"]  # non-empty fallback


def test_run_audit_surfaces_skipped_components_in_report():
    """A graph without ``community`` / INFERRED-confidence attributes will
    skip several probes; those skips must appear in ``skipped_components``
    rather than being invisible."""
    G = nx.Graph()
    G.add_node("a")
    G.add_node("b")
    G.add_edge("a", "b")  # no community attribute, no confidence_score
    audit = run_audit(G, iterations=1)
    sk = audit["skipped_components"]
    assert sk, "expected at least one skipped probe (community/confidence missing)"
    assert any(v.get("grade") == "SKIP" for v in sk.values())
    # Renderer surfaces them too.
    rendered = format_audit_report(audit)
    assert "Skipped probes" in rendered or any(f"`{k}`: SKIP" in rendered for k in sk)
