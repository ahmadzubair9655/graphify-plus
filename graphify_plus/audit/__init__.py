"""graphify_plus.audit — adversarial probes that grade graph quality."""
from graphify_plus.audit.probe import (  # noqa: F401
    run_audit,
    format_audit_report,
    audit_meets_threshold,
    probe_edge_deletion_stability,
    probe_confidence_drift,
    probe_rename_sensitivity,
    probe_structural_fragility,
    probe_lonely_inferred_edges,
    probe_modularity_quality,
    probe_centrality_drift,
)
from graphify_plus.audit.cli import main as cli_main  # noqa: F401
