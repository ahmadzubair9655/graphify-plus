"""v4.4.0 task 0 — drift wiring policy.

When unresolved_imports / phantom_symbols / low_confidence_ratio score
D or F, ``record_from_audit`` appends one drift event per contributing
edge.
"""

from __future__ import annotations

from graphify_plus.audit.drift_log import aggregate, record_from_audit


def test_record_from_audit_no_op_for_grade_C(tmp_path):
    audit = {
        "unresolved_imports": {
            "skipped": False,
            "unresolved_imports_grade": "C",
            "contributing_edges": [("a", "b", "imports")],
        }
    }
    n = record_from_audit(audit, tmp_path)
    assert n == 0


def test_record_from_audit_emits_for_D_and_F(tmp_path):
    audit = {
        "unresolved_imports": {
            "skipped": False,
            "unresolved_imports_grade": "D",
            "contributing_edges": [("a", "b", "imports"), ("a", "c", "imports")],
        },
        "low_confidence_ratio": {
            "skipped": False,
            "low_confidence_grade": "F",
            "contributing_edges": [("x", "y", "calls")],
        },
        "phantom_symbols": {
            "skipped": False,
            "phantom_grade": "F",
            "contributing_edges": [],
        },
    }
    n = record_from_audit(audit, tmp_path)
    assert n == 3
    log = (tmp_path / ".graphify_plus" / "confidence_drift.log").read_text()
    assert "unresolved_imports:D" in log
    assert "low_confidence_ratio:F" in log
    assert aggregate(tmp_path) == {}  # threshold not reached yet
