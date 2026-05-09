"""
graphify-plus — Drop-in enhancement layer for graphify.

Public API:

    from graphify_plus import run_audit, format_audit_report
    from graphify_plus import diff_graphs, format_pr_comment, format_text_diff

The two production-ready surfaces are:

    graphify_plus.audit   — adversarial probes (probe.py: 7 probes + run_audit)
    graphify_plus.review  — graph diff for code review (graph_diff.py + cli.py)

Other modules referenced in the README (temporal, reconcile, causal, contradict,
budget, writeback, report_json, counterfactual, verification, selfhealing,
multimodal, live, federation, local, personal) are planned but not yet shipped.
"""

from __future__ import annotations

__version__ = "6.0.0"

# Re-export the headline functions so callers can do `from graphify_plus import …`
from graphify_plus.audit.probe import (  # noqa: E402,F401
    audit_meets_threshold,
    format_audit_report,
    probe_centrality_drift,
    probe_confidence_drift,
    probe_edge_deletion_stability,
    probe_lonely_inferred_edges,
    probe_modularity_quality,
    probe_rename_sensitivity,
    probe_structural_fragility,
    run_audit,
)
from graphify_plus.review.graph_diff import (  # noqa: E402,F401
    diff_graphs,
    diff_meets_threshold,
    format_pr_comment,
    format_text_diff,
)

__all__ = [
    "__version__",
    # audit
    "run_audit",
    "format_audit_report",
    "audit_meets_threshold",
    "probe_edge_deletion_stability",
    "probe_confidence_drift",
    "probe_rename_sensitivity",
    "probe_structural_fragility",
    "probe_lonely_inferred_edges",
    "probe_modularity_quality",
    "probe_centrality_drift",
    # review
    "diff_graphs",
    "format_pr_comment",
    "format_text_diff",
    "diff_meets_threshold",
]
