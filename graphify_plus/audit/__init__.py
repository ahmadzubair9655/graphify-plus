"""graphify_plus.audit — adversarial probes that grade graph quality."""

from graphify_plus.audit.cli import main as cli_main  # noqa: F401
from graphify_plus.audit.probe import (  # noqa: F401
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
