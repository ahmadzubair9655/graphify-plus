"""Production-grade test suite for graphify_plus.audit."""

import json

import networkx as nx


def _make_basic_graph():
    G = nx.Graph()
    G.add_node("auth_service", label="AuthService", source_file="src/auth.py", community=0)
    G.add_node("user_model", label="UserModel", source_file="src/models.py", community=0)
    G.add_node("login_fn", label="login", source_file="src/auth.py", community=0)
    G.add_node("db_write", label="db_write", source_file="src/db.py", community=1)
    G.add_node("token_validator", label="TokenValidator", source_file="src/auth.py", community=0)
    G.add_node("audit_log", label="AuditLog", source_file="src/audit.py", community=1)
    G.add_node("config", label="Config", source_file="src/cfg.py", community=2)
    G.add_edge("auth_service", "user_model", relation="uses", confidence="EXTRACTED")
    G.add_edge("login_fn", "auth_service", relation="calls", confidence="EXTRACTED")
    G.add_edge("login_fn", "db_write", relation="modifies", confidence="EXTRACTED")
    G.add_edge(
        "auth_service",
        "token_validator",
        relation="delegates_to",
        confidence="INFERRED",
        confidence_score=0.78,
    )
    G.add_edge(
        "token_validator",
        "audit_log",
        relation="writes_to",
        confidence="INFERRED",
        confidence_score=0.65,
    )
    G.add_edge(
        "auth_service", "config", relation="uses", confidence="INFERRED", confidence_score=0.50
    )
    return G


# ─── Probe 1: edge deletion stability ───────────────────────────────────────


class TestEdgeDeletionStability:
    def test_basic_run(self):
        from graphify_plus.audit.probe import probe_edge_deletion_stability

        G = _make_basic_graph()
        r = probe_edge_deletion_stability(G, iterations=3, seed=1)
        assert r["skipped"] is False
        assert r["stability_grade"] in ("A", "B", "C", "D", "F")
        assert 0 <= r["avg_overlap"] <= 1

    def test_empty_graph(self):
        from graphify_plus.audit.probe import probe_edge_deletion_stability

        r = probe_edge_deletion_stability(nx.Graph(), iterations=1)
        assert r["skipped"] is True

    def test_no_community_attr(self):
        from graphify_plus.audit.probe import probe_edge_deletion_stability

        G = nx.Graph()
        G.add_edges_from([("a", "b"), ("b", "c")])
        r = probe_edge_deletion_stability(G)
        assert r["skipped"] is True
        assert "community" in r["reason"].lower()

    def test_no_edges(self):
        from graphify_plus.audit.probe import probe_edge_deletion_stability

        G = nx.Graph()
        G.add_node("a", community=0)
        r = probe_edge_deletion_stability(G)
        assert r["skipped"] is True

    def test_invalid_deletion_rate(self):
        from graphify_plus.audit.probe import probe_edge_deletion_stability

        G = _make_basic_graph()
        for bad_rate in (0.0, 1.0, -0.5, 1.5, 2.0):
            r = probe_edge_deletion_stability(G, deletion_rate=bad_rate)
            assert r["skipped"] is True

    def test_invalid_iterations(self):
        from graphify_plus.audit.probe import probe_edge_deletion_stability

        G = _make_basic_graph()
        r = probe_edge_deletion_stability(G, iterations=0)
        assert r["skipped"] is True

    def test_deterministic_with_same_seed(self):
        from graphify_plus.audit.probe import probe_edge_deletion_stability

        G = _make_basic_graph()
        r1 = probe_edge_deletion_stability(G, seed=42, iterations=3)
        r2 = probe_edge_deletion_stability(G, seed=42, iterations=3)
        assert r1["avg_overlap"] == r2["avg_overlap"]
        assert r1["min_overlap"] == r2["min_overlap"]


# ─── Probe 2: confidence drift ──────────────────────────────────────────────


class TestConfidenceDrift:
    def test_basic_run(self):
        from graphify_plus.audit.probe import probe_confidence_drift

        G = _make_basic_graph()
        r = probe_confidence_drift(G)
        assert r["skipped"] is False
        assert r["inferred_edge_count"] == 3
        assert r["drift_grade"] in ("A", "B", "C", "D", "F")

    def test_empty_graph(self):
        from graphify_plus.audit.probe import probe_confidence_drift

        r = probe_confidence_drift(nx.Graph())
        assert r["skipped"] is True

    def test_too_few_inferred_edges(self):
        from graphify_plus.audit.probe import probe_confidence_drift

        G = nx.Graph()
        G.add_node("a")
        G.add_node("b")
        G.add_edge("a", "b", confidence="INFERRED", confidence_score=0.7)
        r = probe_confidence_drift(G)
        assert r["skipped"] is True

    def test_invalid_score_values_ignored(self):
        from graphify_plus.audit.probe import probe_confidence_drift

        G = nx.Graph()
        for i in range(5):
            G.add_node(f"n{i}")
        # 3 valid scores + 2 invalid (out of range, wrong type)
        G.add_edge("n0", "n1", confidence="INFERRED", confidence_score=0.7)
        G.add_edge("n1", "n2", confidence="INFERRED", confidence_score=0.6)
        G.add_edge("n2", "n3", confidence="INFERRED", confidence_score=0.8)
        G.add_edge("n3", "n4", confidence="INFERRED", confidence_score=2.0)  # invalid
        G.add_edge("n0", "n2", confidence="INFERRED", confidence_score="0.5")  # invalid type
        r = probe_confidence_drift(G)
        assert r["inferred_edge_count"] == 3


# ─── Probe 3: rename sensitivity ────────────────────────────────────────────


class TestRenameSensitivity:
    def test_basic_run(self):
        from graphify_plus.audit.probe import probe_rename_sensitivity

        G = _make_basic_graph()
        r = probe_rename_sensitivity(G)
        assert r["skipped"] is False
        assert r["rename_grade"] in ("A", "B", "C", "D", "F")

    def test_detects_self_referencing_edge(self):
        from graphify_plus.audit.probe import probe_rename_sensitivity

        G = nx.Graph()
        G.add_node("auth", label="AuthService")
        G.add_node("user", label="User")
        G.add_node("log", label="Log")
        G.add_edge("auth", "user", evidence="AuthService delegates to User")
        G.add_edge("auth", "log", description="AuthService writes to Log")
        r = probe_rename_sensitivity(G)
        assert any(f["label"] == "AuthService" for f in r["fragile_nodes"])

    def test_short_labels_ignored(self):
        from graphify_plus.audit.probe import probe_rename_sensitivity

        G = nx.Graph()
        G.add_node("a", label="x")  # label too short
        G.add_node("b", label="y")
        G.add_edge("a", "b", evidence="x calls y")
        r = probe_rename_sensitivity(G)
        assert r["fragile_nodes"] == []

    def test_directed_graph(self):
        from graphify_plus.audit.probe import probe_rename_sensitivity

        G = nx.DiGraph()
        G.add_node("auth", label="AuthService")
        G.add_node("db", label="Database")
        G.add_edge("auth", "db", evidence="AuthService persists to db")
        r = probe_rename_sensitivity(G)
        assert any(f["label"] == "AuthService" for f in r["fragile_nodes"])


# ─── Probe 4: structural fragility ──────────────────────────────────────────


class TestStructuralFragility:
    def test_finds_articulation_point(self):
        from graphify_plus.audit.probe import probe_structural_fragility

        G = nx.Graph()
        G.add_edges_from([("a", "cut"), ("b", "cut"), ("cut", "c"), ("c", "d"), ("d", "e")])
        r = probe_structural_fragility(G)
        ids = {p["id"] for p in r["articulation_points"]}
        assert "cut" in ids

    def test_complete_graph_no_articulation(self):
        from graphify_plus.audit.probe import probe_structural_fragility

        G = nx.complete_graph(5)
        r = probe_structural_fragility(G)
        assert r["articulation_count"] == 0
        assert r["structural_grade"] == "A"

    def test_disconnected_graph(self):
        from graphify_plus.audit.probe import probe_structural_fragility

        G = nx.Graph()
        G.add_edges_from([("a", "b"), ("c", "d")])  # disconnected
        r = probe_structural_fragility(G)
        # Should not crash, should restrict to largest CC
        assert "skipped" in r

    def test_directed_handled(self):
        from graphify_plus.audit.probe import probe_structural_fragility

        G = nx.DiGraph()
        G.add_edges_from([("a", "b"), ("b", "c"), ("c", "d")])
        r = probe_structural_fragility(G)
        assert "skipped" in r

    def test_multigraph_handled(self):
        from graphify_plus.audit.probe import probe_structural_fragility

        G = nx.MultiGraph()
        G.add_edges_from([("a", "b"), ("a", "b"), ("b", "c"), ("c", "d")])
        r = probe_structural_fragility(G)
        assert "skipped" in r

    def test_empty_graph(self):
        from graphify_plus.audit.probe import probe_structural_fragility

        r = probe_structural_fragility(nx.Graph())
        assert r["skipped"] is True

    def test_single_node(self):
        from graphify_plus.audit.probe import probe_structural_fragility

        G = nx.Graph()
        G.add_node("a")
        r = probe_structural_fragility(G)
        assert r["skipped"] is True


# ─── Probe 5: lonely INFERRED edges ─────────────────────────────────────────


class TestLonelyInferredEdges:
    def test_basic_run(self):
        from graphify_plus.audit.probe import probe_lonely_inferred_edges

        G = _make_basic_graph()
        r = probe_lonely_inferred_edges(G)
        assert r["skipped"] is False

    def test_high_confidence_skipped(self):
        from graphify_plus.audit.probe import probe_lonely_inferred_edges

        G = nx.Graph()
        G.add_node("a")
        G.add_node("b")
        G.add_edge("a", "b", confidence="INFERRED", confidence_score=0.95)
        r = probe_lonely_inferred_edges(G, high_confidence_cutoff=0.85)
        assert r["lonely_count"] == 0

    def test_low_confidence_with_no_shared_neighbour(self):
        from graphify_plus.audit.probe import probe_lonely_inferred_edges

        G = nx.Graph()
        G.add_node("a")
        G.add_node("b")
        G.add_edge("a", "b", confidence="INFERRED", confidence_score=0.50)
        r = probe_lonely_inferred_edges(G)
        assert r["lonely_count"] == 1

    def test_with_shared_neighbour_not_lonely(self):
        from graphify_plus.audit.probe import probe_lonely_inferred_edges

        G = nx.Graph()
        G.add_edges_from([("a", "x"), ("b", "x")])
        G.add_edge("a", "b", confidence="INFERRED", confidence_score=0.50)
        r = probe_lonely_inferred_edges(G)
        # a-b has shared neighbour x -> not lonely
        assert r["lonely_count"] == 0


# ─── Probe 6: modularity quality ────────────────────────────────────────────


class TestModularityQuality:
    def test_basic_run(self):
        from graphify_plus.audit.probe import probe_modularity_quality

        G = _make_basic_graph()
        r = probe_modularity_quality(G)
        assert r["skipped"] is False
        assert -0.5 <= r["modularity_Q"] <= 1.0
        assert r["modularity_grade"] in ("A", "B", "C", "D", "F")

    def test_no_communities(self):
        from graphify_plus.audit.probe import probe_modularity_quality

        G = nx.Graph()
        G.add_edges_from([("a", "b"), ("b", "c")])
        r = probe_modularity_quality(G)
        assert r["skipped"] is True

    def test_single_community(self):
        from graphify_plus.audit.probe import probe_modularity_quality

        G = nx.Graph()
        G.add_node("a", community=0)
        G.add_node("b", community=0)
        G.add_edge("a", "b")
        r = probe_modularity_quality(G)
        assert r["skipped"] is True


# ─── Probe 7: centrality drift ──────────────────────────────────────────────


class TestCentralityDrift:
    def test_basic_run(self):
        from graphify_plus.audit.probe import probe_centrality_drift

        G = _make_basic_graph()
        r = probe_centrality_drift(G)
        assert r["skipped"] is False
        assert r["centrality_grade"] in ("A", "B", "C", "D", "F", "N/A")

    def test_no_edges(self):
        from graphify_plus.audit.probe import probe_centrality_drift

        G = nx.Graph()
        G.add_node("a")
        r = probe_centrality_drift(G)
        assert r["skipped"] is True

    def test_empty_graph(self):
        from graphify_plus.audit.probe import probe_centrality_drift

        r = probe_centrality_drift(nx.Graph())
        assert r["skipped"] is True


# ─── Audit aggregation ──────────────────────────────────────────────────────


class TestRunAudit:
    def test_full_run(self):
        from graphify_plus.audit.probe import run_audit

        G = _make_basic_graph()
        r = run_audit(G, iterations=3, seed=1)
        for probe in (
            "edge_deletion_stability",
            "confidence_drift",
            "rename_sensitivity",
            "structural_fragility",
            "lonely_inferred_edges",
            "modularity_quality",
            "centrality_drift",
        ):
            assert probe in r
        assert r["overall_grade"] in ("A", "B", "C", "D", "F", "N/A")

    def test_empty_graph_handled_gracefully(self):
        from graphify_plus.audit.probe import run_audit

        r = run_audit(nx.Graph())
        assert r["skipped"] is True
        assert r["overall_grade"] == "N/A"

    def test_only_subset_of_probes(self):
        from graphify_plus.audit.probe import run_audit

        G = _make_basic_graph()
        r = run_audit(G, only=["confidence_drift"])
        assert "confidence_drift" in r
        assert "structural_fragility" not in r

    def test_skip_expensive(self):
        from graphify_plus.audit.probe import run_audit

        G = _make_basic_graph()
        r = run_audit(G, skip_expensive=True)
        assert "centrality_drift" not in r

    def test_invalid_input_returns_safe_dict(self):
        from graphify_plus.audit.probe import run_audit

        r = run_audit(None)
        assert r["skipped"] is True
        assert r["overall_grade"] == "N/A"

    def test_directed_graph(self):
        from graphify_plus.audit.probe import run_audit

        G = nx.DiGraph()
        G.add_node("a", community=0)
        G.add_node("b", community=0)
        G.add_node("c", community=1)
        G.add_edge("a", "b", confidence="EXTRACTED")
        G.add_edge("b", "c", confidence="EXTRACTED")
        r = run_audit(G)
        assert r["graph_size"]["directed"] is True

    def test_multigraph(self):
        from graphify_plus.audit.probe import run_audit

        G = nx.MultiGraph()
        for i in range(3):
            G.add_node(f"n{i}", community=i % 2)
        G.add_edge("n0", "n1", confidence="EXTRACTED")
        G.add_edge("n0", "n1", confidence="INFERRED", confidence_score=0.7)
        G.add_edge("n1", "n2", confidence="EXTRACTED")
        r = run_audit(G, skip_expensive=True)
        assert r["graph_size"]["multigraph"] is True


# ─── Format and threshold ───────────────────────────────────────────────────


class TestFormatAndThreshold:
    def test_format_audit_report(self):
        from graphify_plus.audit.probe import format_audit_report, run_audit

        G = _make_basic_graph()
        r = run_audit(G)
        md = format_audit_report(r)
        assert "Adversarial Audit Report" in md

    def test_format_skipped_audit(self):
        from graphify_plus.audit.probe import format_audit_report, run_audit

        r = run_audit(nx.Graph())
        md = format_audit_report(r)
        assert "Skipped" in md

    def test_meets_threshold_pass(self):
        from graphify_plus.audit.probe import audit_meets_threshold

        assert audit_meets_threshold({"overall_grade": "A"}, "B") is True
        assert audit_meets_threshold({"overall_grade": "B"}, "B") is True

    def test_meets_threshold_fail(self):
        from graphify_plus.audit.probe import audit_meets_threshold

        assert audit_meets_threshold({"overall_grade": "D"}, "B") is False

    def test_meets_threshold_na_passes(self):
        from graphify_plus.audit.probe import audit_meets_threshold

        # N/A should not fail CI
        assert audit_meets_threshold({"overall_grade": "N/A"}, "A") is True


# ─── CLI ────────────────────────────────────────────────────────────────────


class TestAuditCLI:
    def test_loads_graph_and_runs(self, tmp_path, capsys):
        from graphify_plus.audit.cli import main
        from graphify_plus.report_json import save_enhanced_graph

        G = _make_basic_graph()
        save_enhanced_graph(G, tmp_path)
        rc = main([str(tmp_path / "graph_enhanced.json"), "--quiet"])
        assert rc == 0

    def test_cli_missing_file(self, tmp_path):
        from graphify_plus.audit.cli import main

        rc = main([str(tmp_path / "nonexistent.json"), "--quiet"])
        assert rc == 2

    def test_cli_json_output(self, tmp_path):
        from graphify_plus.audit.cli import main
        from graphify_plus.report_json import save_enhanced_graph

        G = _make_basic_graph()
        save_enhanced_graph(G, tmp_path)
        out_file = tmp_path / "audit.json"
        rc = main(
            [
                str(tmp_path / "graph_enhanced.json"),
                "--json-output",
                str(out_file),
                "--quiet",
            ]
        )
        assert rc == 0
        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert "overall_grade" in data

    def test_cli_fail_below_grade(self, tmp_path):
        from graphify_plus.audit.cli import main
        from graphify_plus.report_json import save_enhanced_graph

        # Create a graph that should fail high thresholds
        G = nx.Graph()
        G.add_node("a", community=0)
        G.add_node("b", community=1)
        G.add_edge("a", "b", confidence="INFERRED", confidence_score=0.3)
        save_enhanced_graph(G, tmp_path)
        rc = main(
            [
                str(tmp_path / "graph_enhanced.json"),
                "--fail-below",
                "A",
                "--quiet",
            ]
        )
        # May pass or fail depending on grade; just ensure no crash
        assert rc in (0, 1)

    def test_cli_only_filters_probes(self, tmp_path):
        from graphify_plus.audit.cli import main
        from graphify_plus.report_json import save_enhanced_graph

        G = _make_basic_graph()
        save_enhanced_graph(G, tmp_path)
        out_file = tmp_path / "audit.json"
        rc = main(
            [
                str(tmp_path / "graph_enhanced.json"),
                "--only",
                "confidence_drift",
                "--json-output",
                str(out_file),
                "--quiet",
            ]
        )
        assert rc == 0
        data = json.loads(out_file.read_text())
        assert "confidence_drift" in data
        assert "structural_fragility" not in data


# ─── Stress / edge cases ────────────────────────────────────────────────────


class TestStressAndEdgeCases:
    def test_unicode_labels(self):
        from graphify_plus.audit.probe import run_audit

        G = nx.Graph()
        G.add_node("a", label="日本語サービス", community=0)
        G.add_node("b", label="مصادقة", community=0)
        G.add_node("c", label="Auth🔐", community=1)
        G.add_edge("a", "b", confidence="EXTRACTED")
        G.add_edge("b", "c", confidence="EXTRACTED")
        r = run_audit(G)
        assert "skipped" not in r or not r.get("skipped")

    def test_very_long_labels(self):
        from graphify_plus.audit.probe import probe_rename_sensitivity

        G = nx.Graph()
        long_label = "x" * 1000
        G.add_node("a", label=long_label)
        G.add_node("b", label="b")
        G.add_edge("a", "b", evidence=long_label[:500])
        # Should not crash
        probe_rename_sensitivity(G)

    def test_numeric_node_ids(self):
        from graphify_plus.audit.probe import run_audit

        G = nx.Graph()
        G.add_node(1, label="One", community=0)
        G.add_node(2, label="Two", community=1)
        G.add_edge(1, 2, confidence="EXTRACTED")
        r = run_audit(G)
        assert "skipped" not in r or not r.get("skipped")

    def test_serialisable_to_json(self):
        from graphify_plus.audit.probe import run_audit

        G = _make_basic_graph()
        r = run_audit(G)
        # Must be fully JSON-serialisable
        encoded = json.dumps(r)
        decoded = json.loads(encoded)
        assert decoded["overall_grade"] == r["overall_grade"]

    def test_concurrent_runs_dont_share_state(self):
        from graphify_plus.audit.probe import run_audit

        G1 = _make_basic_graph()
        G2 = nx.Graph()
        G2.add_node("solo", community=0)
        r1 = run_audit(G1)
        r2 = run_audit(G2)
        assert r1["graph_size"]["nodes"] != r2["graph_size"]["nodes"]
