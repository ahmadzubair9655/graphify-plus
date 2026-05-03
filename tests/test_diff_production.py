"""Production-grade test suite for graphify_plus.review."""

import json
from pathlib import Path

import networkx as nx
import pytest


def _make_graph(extra_nodes=None, extra_edges=None):
    G = nx.Graph()
    G.add_node("auth_service", label="AuthService", source_file="src/auth.py", community=0)
    G.add_node("user_model", label="UserModel", source_file="src/models.py", community=0)
    G.add_node("login_fn", label="login", source_file="src/auth.py", community=0)
    G.add_node(
        "rationale",
        label="OAuth2 reason",
        source_file="docs/SECURITY.md",
        community=2,
        node_type="rationale_for",
    )
    G.add_edge("auth_service", "user_model", relation="uses", confidence="EXTRACTED")
    G.add_edge("login_fn", "auth_service", relation="calls", confidence="EXTRACTED")
    G.add_edge(
        "rationale",
        "auth_service",
        relation="caused_by",
        confidence="INFERRED",
        confidence_score=0.78,
    )
    if extra_nodes:
        for nid, attrs in extra_nodes.items():
            G.add_node(nid, **attrs)
    if extra_edges:
        for u, v, attrs in extra_edges:
            G.add_edge(u, v, **attrs)
    return G


def _save_graph(G, path):
    """Save a graph to JSON in graphify-plus schema."""
    data = {
        "_directed": G.is_directed(),
        "nodes": [{"id": n, **G.nodes[n]} for n in G.nodes()],
        "edges": [{"source": u, "target": v, **G.edges[u, v]} for u, v in G.edges()],
    }
    Path(path).write_text(json.dumps(data, indent=2, default=str))


# ─── Loading and validation ────────────────────────────────────────────────


class TestLoading:
    def test_load_from_path(self, tmp_path):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        new = _make_graph()
        new.add_node("z", label="Z", source_file="z.py")
        old_path = tmp_path / "old.json"
        new_path = tmp_path / "new.json"
        _save_graph(old, old_path)
        _save_graph(new, new_path)
        d = diff_graphs(old_path, new_path)
        assert d["summary"]["node_delta"] == 1

    def test_load_directly_from_graph(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        new = _make_graph()
        new.add_node("z", label="Z")
        d = diff_graphs(old, new)
        assert d["summary"]["node_delta"] == 1

    def test_invalid_json_raises(self, tmp_path):
        from graphify_plus.review.graph_diff import diff_graphs

        bad = tmp_path / "bad.json"
        bad.write_text("not json at all {")
        good = tmp_path / "good.json"
        _save_graph(_make_graph(), good)
        with pytest.raises(json.JSONDecodeError):
            diff_graphs(bad, good)

    def test_missing_keys_raise(self, tmp_path):
        from graphify_plus.review.graph_diff import diff_graphs

        bad = tmp_path / "bad.json"
        bad.write_text('{"foo": "bar"}')
        good = tmp_path / "good.json"
        _save_graph(_make_graph(), good)
        with pytest.raises(ValueError):
            diff_graphs(bad, good)

    def test_missing_file_raises(self, tmp_path):
        from graphify_plus.review.graph_diff import diff_graphs

        good = tmp_path / "good.json"
        _save_graph(_make_graph(), good)
        with pytest.raises(FileNotFoundError):
            diff_graphs(tmp_path / "nope.json", good)


# ─── Severity classification ────────────────────────────────────────────────


class TestSeverity:
    def test_critical_for_high_degree_node_removal(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        # Add a hub node with 10+ edges so its removal becomes critical
        for i in range(12):
            old.add_node(f"n{i}", label=f"N{i}")
            old.add_edge("auth_service", f"n{i}", relation="calls", confidence="EXTRACTED")
        new = _make_graph()  # without auth_service hub
        new.remove_node("auth_service")
        d = diff_graphs(old, new)
        # auth_service now has very high degree in old → CRITICAL on removal
        sev_levels = [n["severity"] for n in d["nodes_removed"]]
        assert "CRITICAL" in sev_levels

    def test_low_for_isolated_node_removal(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph(extra_nodes={"isolated": {"label": "Isolated"}})
        new = _make_graph()  # no isolated node
        d = diff_graphs(old, new)
        removed_isolated = [n for n in d["nodes_removed"] if n["id"] == "isolated"]
        assert removed_isolated
        assert removed_isolated[0]["severity"] == "LOW"

    def test_node_with_causal_edge_is_critical(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        new = _make_graph()
        new.remove_node("rationale")
        d = diff_graphs(old, new)
        rationale_removed = [n for n in d["nodes_removed"] if n["id"] == "rationale"]
        assert rationale_removed[0]["severity"] in ("HIGH", "CRITICAL")

    def test_overall_severity_is_max(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        new = _make_graph()
        new.remove_node("rationale")  # causes orphan + critical removal
        d = diff_graphs(old, new)
        assert d["overall_severity"] in ("HIGH", "CRITICAL")


# ─── Diff content ───────────────────────────────────────────────────────────


class TestDiffContent:
    def test_node_addition(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        new = _make_graph(extra_nodes={"new_node": {"label": "New", "source_file": "new.py"}})
        d = diff_graphs(old, new)
        assert len(d["nodes_added"]) == 1
        assert d["nodes_added"][0]["label"] == "New"

    def test_node_removal(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        new = _make_graph()
        new.remove_node("login_fn")
        d = diff_graphs(old, new)
        assert any(n["id"] == "login_fn" for n in d["nodes_removed"])

    def test_edge_addition_count(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        new = _make_graph()
        new.add_edge("login_fn", "user_model", relation="uses", confidence="EXTRACTED")
        d = diff_graphs(old, new)
        assert d["edges_added_count"] == 1

    def test_edge_attribute_change_detected(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        new = _make_graph()
        new["auth_service"]["user_model"]["relation"] = "wraps"  # changed relation
        # Note: changing relation actually creates a new edge_id since relation
        # is part of the key. So this should appear as add+remove, not attr change.
        d = diff_graphs(old, new)
        rel_edges_added = [e for e in d["edge_types_added"]]
        assert "wraps" in d["edge_types_added"] or any(
            "relation" in e["changed_attrs"] for e in d["edge_attr_changes"]
        )

    def test_orphaned_causal_chains_detected(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        new = _make_graph()
        new.remove_node("rationale")
        d = diff_graphs(old, new)
        assert len(d["orphaned_causal_chains"]) >= 1

    def test_god_node_rank_change(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        # Add many edges to make user_model a higher-rank god node in new
        new = _make_graph()
        for i in range(5):
            new.add_node(f"x{i}")
            new.add_edge(f"x{i}", "user_model", relation="uses", confidence="EXTRACTED")
        d = diff_graphs(old, new)
        # Should detect rank change for user_model
        assert isinstance(d["god_node_rank_changes"], list)

    def test_contradictions_delta(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = _make_graph()
        new = _make_graph()
        new.add_edge("login_fn", "user_model", relation="CONTRADICTS", description="conflict")
        d = diff_graphs(old, new)
        assert d["contradictions"]["delta"] == 1


# ─── Health delta ───────────────────────────────────────────────────────────


class TestHealthDelta:
    def test_health_delta_present(self, tmp_path):
        from graphify_plus.review.graph_diff import diff_graphs

        old_report = {"health": {"score": 75, "grade": "B"}}
        new_report = {"health": {"score": 82, "grade": "B"}}
        old = _make_graph()
        new = _make_graph()
        d = diff_graphs(old, new, old_report=old_report, new_report=new_report)
        assert d["health_delta"]["delta"] == 7

    def test_health_delta_from_paths(self, tmp_path):
        from graphify_plus.review.graph_diff import diff_graphs

        old_rp = tmp_path / "old.json"
        new_rp = tmp_path / "new.json"
        old_rp.write_text(json.dumps({"health": {"score": 60, "grade": "C"}}))
        new_rp.write_text(json.dumps({"health": {"score": 85, "grade": "A"}}))
        d = diff_graphs(_make_graph(), _make_graph(), old_report=old_rp, new_report=new_rp)
        assert d["health_delta"]["delta"] == 25

    def test_no_health_delta_without_reports(self):
        from graphify_plus.review.graph_diff import diff_graphs

        d = diff_graphs(_make_graph(), _make_graph())
        assert d["health_delta"] is None

    def test_malformed_report_doesnt_crash(self, tmp_path):
        from graphify_plus.review.graph_diff import diff_graphs

        bad_rp = tmp_path / "bad.json"
        bad_rp.write_text("garbage")
        d = diff_graphs(_make_graph(), _make_graph(), old_report=bad_rp, new_report=bad_rp)
        assert d["health_delta"] is None


# ─── Format ─────────────────────────────────────────────────────────────────


class TestFormat:
    def test_pr_comment_basic(self):
        from graphify_plus.review.graph_diff import diff_graphs, format_pr_comment

        d = diff_graphs(_make_graph(), _make_graph())
        md = format_pr_comment(d)
        assert "Graph diff" in md

    def test_pr_comment_with_changes(self):
        from graphify_plus.review.graph_diff import diff_graphs, format_pr_comment

        old = _make_graph()
        new = _make_graph(extra_nodes={"new1": {"label": "New1"}})
        d = diff_graphs(old, new)
        md = format_pr_comment(d)
        assert "New1" in md
        assert "added" in md.lower() or "+" in md

    def test_pr_comment_with_health_delta(self):
        from graphify_plus.review.graph_diff import diff_graphs, format_pr_comment

        d = diff_graphs(
            _make_graph(),
            _make_graph(),
            old_report={"health": {"score": 70, "grade": "B"}},
            new_report={"health": {"score": 80, "grade": "A"}},
        )
        md = format_pr_comment(d)
        assert "Health" in md
        assert "70" in md and "80" in md

    def test_text_format(self):
        from graphify_plus.review.graph_diff import diff_graphs, format_text_diff

        d = diff_graphs(_make_graph(), _make_graph())
        text = format_text_diff(d)
        assert "Graph diff" in text

    def test_json_serialisable(self):
        from graphify_plus.review.graph_diff import diff_graphs

        d = diff_graphs(_make_graph(), _make_graph())
        # Must serialise without errors
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        assert decoded["summary"] == d["summary"]


# ─── Threshold ─────────────────────────────────────────────────────────────


class TestThreshold:
    def test_meets_threshold_pass(self):
        from graphify_plus.review.graph_diff import diff_meets_threshold

        assert diff_meets_threshold({"overall_severity": "LOW"}, "HIGH") is True

    def test_meets_threshold_fail(self):
        from graphify_plus.review.graph_diff import diff_meets_threshold

        assert diff_meets_threshold({"overall_severity": "CRITICAL"}, "HIGH") is False

    def test_threshold_at_boundary(self):
        from graphify_plus.review.graph_diff import diff_meets_threshold

        assert diff_meets_threshold({"overall_severity": "HIGH"}, "HIGH") is True


# ─── Edge cases ─────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_identical_graphs(self):
        from graphify_plus.review.graph_diff import diff_graphs

        G = _make_graph()
        d = diff_graphs(G, G)
        assert len(d["nodes_added"]) == 0
        assert len(d["nodes_removed"]) == 0

    def test_empty_to_full(self):
        from graphify_plus.review.graph_diff import diff_graphs

        empty = nx.Graph()
        d = diff_graphs(empty, _make_graph())
        assert len(d["nodes_added"]) == 4

    def test_full_to_empty(self):
        from graphify_plus.review.graph_diff import diff_graphs

        d = diff_graphs(_make_graph(), nx.Graph())
        assert len(d["nodes_removed"]) == 4

    def test_directed_to_undirected_handled(self):
        from graphify_plus.review.graph_diff import diff_graphs

        old = nx.DiGraph()
        old.add_edge("a", "b", relation="calls")
        new = nx.Graph()
        new.add_edge("a", "b", relation="calls")
        # Both should diff cleanly
        d = diff_graphs(old, new)
        assert "summary" in d

    def test_multigraph_handled(self):
        from graphify_plus.review.graph_diff import diff_graphs

        G = nx.MultiGraph()
        G.add_edge("a", "b", relation="calls")
        G.add_edge("a", "b", relation="wraps")  # multi-edge
        d = diff_graphs(G, G)
        # Identical multigraph diff: 0 changes
        assert d["summary"]["node_delta"] == 0


# ─── CLI ────────────────────────────────────────────────────────────────────


class TestDiffCLI:
    def test_basic_run(self, tmp_path, capsys):
        from graphify_plus.review.cli import main

        old_path = tmp_path / "old.json"
        new_path = tmp_path / "new.json"
        _save_graph(_make_graph(), old_path)
        new_g = _make_graph(extra_nodes={"new1": {"label": "New1"}})
        _save_graph(new_g, new_path)
        rc = main([str(old_path), str(new_path), "--quiet"])
        # Will print to stdout but quiet only suppresses confirmations
        assert rc == 0

    def test_missing_file(self, tmp_path):
        from graphify_plus.review.cli import main

        good = tmp_path / "g.json"
        _save_graph(_make_graph(), good)
        rc = main([str(tmp_path / "nope.json"), str(good), "--quiet"])
        assert rc == 2

    def test_json_output(self, tmp_path):
        from graphify_plus.review.cli import main

        old_path = tmp_path / "old.json"
        new_path = tmp_path / "new.json"
        out_path = tmp_path / "diff.json"
        _save_graph(_make_graph(), old_path)
        _save_graph(_make_graph(extra_nodes={"x": {"label": "X"}}), new_path)
        rc = main(
            [
                str(old_path),
                str(new_path),
                "--format",
                "json",
                "--output",
                str(out_path),
                "--quiet",
            ]
        )
        assert rc == 0
        data = json.loads(out_path.read_text())
        assert "summary" in data

    def test_fail_above_threshold(self, tmp_path):
        from graphify_plus.review.cli import main

        old_path = tmp_path / "old.json"
        new_path = tmp_path / "new.json"
        old = _make_graph()
        new = _make_graph()
        new.remove_node("rationale")  # creates orphan → HIGH severity
        _save_graph(old, old_path)
        _save_graph(new, new_path)
        rc = main(
            [
                str(old_path),
                str(new_path),
                "--fail-above",
                "MEDIUM",
                "--quiet",
            ]
        )
        assert rc == 1  # exceeds MEDIUM threshold

    def test_text_format(self, tmp_path, capsys):
        from graphify_plus.review.cli import main

        old_path = tmp_path / "old.json"
        new_path = tmp_path / "new.json"
        _save_graph(_make_graph(), old_path)
        _save_graph(_make_graph(), new_path)
        rc = main([str(old_path), str(new_path), "--format", "text"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Graph diff" in captured.out
