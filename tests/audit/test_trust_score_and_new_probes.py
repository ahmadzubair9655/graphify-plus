"""11.7 — trust score banner + new probes."""

from __future__ import annotations

import networkx as nx

from graphify_plus.audit.probe import (
    format_audit_report,
    probe_low_confidence_ratio,
    probe_phantom_symbols,
    probe_unresolved_imports,
    run_audit,
)


def _toy_graph() -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    g.add_node("m", kind="module", qualified_name="m", language="python", path="m.py", name="m")
    g.add_node("f", kind="function", qualified_name="m.f", language="python", path="m.py", name="f")
    g.add_node(
        "g_orphan",
        kind="function",
        qualified_name="m.orphan",
        language="python",
        path="m.py",
        name="orphan",
    )
    g.add_edge("m", "f", kind="contains", resolved=True, confidence=1.0)
    g.add_edge("m", "ext", kind="imports", resolved=False, confidence=0.2)
    return g


def test_unresolved_imports_probe():
    g = _toy_graph()
    res = probe_unresolved_imports(g)
    assert res["total_imports"] == 1
    assert res["unresolved_count"] == 1
    assert res["unresolved_imports_grade"] in {"A", "B", "C", "D", "F"}


def test_phantom_symbols_probe_finds_orphans():
    g = _toy_graph()
    res = probe_phantom_symbols(g)
    assert res["phantom_count"] >= 1


def test_low_confidence_ratio_probe():
    g = _toy_graph()
    res = probe_low_confidence_ratio(g, threshold=0.7)
    assert res["total_edges_with_confidence"] == 2
    assert res["low_confidence_count"] == 1


def test_run_audit_includes_trust_score_in_0_100_range():
    g = _toy_graph()
    audit = run_audit(g, iterations=2)
    assert "trust_score" in audit
    ts = audit["trust_score"]
    assert isinstance(ts, int)
    assert 0 <= ts <= 100


def test_format_audit_report_renders_trust_banner():
    g = _toy_graph()
    audit = run_audit(g, iterations=2)
    report = format_audit_report(audit)
    assert "Trust score" in report
    assert "/ 100" in report
