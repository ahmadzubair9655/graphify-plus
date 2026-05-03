"""
tests/test_enhancements.py — Full test suite including all 9 new fixes.
Run with: pytest tests/ -v
"""
import json
from pathlib import Path
import networkx as nx
import pytest


def _make_graph(directed=False) -> nx.Graph:
    G = nx.DiGraph() if directed else nx.Graph()
    G.add_node("auth_service", label="AuthService", source_file="src/auth.py",
               node_type="class", community=0)
    G.add_node("user_model", label="UserModel", source_file="src/models.py",
               node_type="class", community=0)
    G.add_node("login_fn", label="login()", source_file="src/auth.py",
               node_type="function", community=0,
               docstring="Idempotent login handler for returning users.")
    G.add_node("db_write", label="db.write()", source_file="src/db.py",
               node_type="function", community=1)
    G.add_node("security_doc", label="OAuth2 decision rationale",
               source_file="docs/SECURITY.md", node_type="rationale_for", community=2)
    G.add_edge("auth_service", "user_model", relation="uses", confidence="EXTRACTED")
    G.add_edge("login_fn", "auth_service", relation="calls", confidence="EXTRACTED")
    G.add_edge("login_fn", "db_write", relation="modifies", confidence="EXTRACTED")
    G.add_edge("security_doc", "auth_service",
               relation="semantically_similar_to", confidence="INFERRED", confidence_score=0.82)
    return G


# ── Fix C: bulk git log ──────────────────────────────────────────────────────

class TestTemporalBulk:
    def test_no_git_repo(self, tmp_path):
        from graphify_enhanced.temporal import enrich_graph_with_temporal
        G = _make_graph()
        result = enrich_graph_with_temporal(G, corpus_root=tmp_path, repo_root=None)
        assert result["skipped_no_git"] is True

    def test_returns_expected_keys(self, tmp_path):
        from graphify_enhanced.temporal import enrich_graph_with_temporal
        G = _make_graph()
        result = enrich_graph_with_temporal(G, corpus_root=tmp_path)
        for k in ("nodes_enriched","supersedes_edges_added","skipped_no_git"):
            assert k in result

    def test_build_file_commit_index_empty_on_non_repo(self, tmp_path):
        from graphify_enhanced.temporal import _build_file_commit_index
        index = _build_file_commit_index(tmp_path)
        assert isinstance(index, dict)

    def test_get_timeline_sorted(self):
        from graphify_enhanced.temporal import get_timeline
        G = nx.Graph()
        G.add_node("a", label="A", first_seen="2024-01-01T00:00:00+00:00", source_file="a.py")
        G.add_node("b", label="B", first_seen="2023-06-01T00:00:00+00:00", source_file="b.py")
        t = get_timeline(G)
        assert t[0]["id"] == "b"


# ── Fix B: auto-namespace tagging ────────────────────────────────────────────

class TestAutoNamespace:
    def test_auto_tag_flat_paths(self):
        from graphify_enhanced.reconcile import auto_tag_namespaces
        G = nx.Graph()
        G.add_node("a", label="A", source_file="repo-A/src/auth.py")
        G.add_node("b", label="B", source_file="repo-B/src/auth.py")
        summary = auto_tag_namespaces(G)
        assert G.nodes["a"].get("namespace") == "repo-A"
        assert G.nodes["b"].get("namespace") == "repo-B"
        assert "repo-A" in summary

    def test_auto_tag_no_source_file(self):
        from graphify_enhanced.reconcile import auto_tag_namespaces
        G = nx.Graph()
        G.add_node("a", label="A")
        # Should not raise
        auto_tag_namespaces(G)

    def test_reconcile_uses_auto_tag(self):
        from graphify_enhanced.reconcile import reconcile_merged_graph
        G = nx.Graph()
        G.add_node("a", label="AuthService", source_file="repo-A/auth.py")
        G.add_node("b", label="AuthService", source_file="repo-B/auth.py")
        result = reconcile_merged_graph(G, llm_fn=None, auto_tag=True)
        assert result["edges_added"] == 1

    def test_reconcile_auto_tag_false_no_tag(self):
        from graphify_enhanced.reconcile import reconcile_merged_graph
        G = nx.Graph()
        G.add_node("a", label="AuthService", source_file="repo-A/auth.py")
        G.add_node("b", label="AuthService", source_file="repo-B/auth.py")
        # auto_tag=False, no namespace set → all land in __default__ → 0 pairs
        result = reconcile_merged_graph(G, llm_fn=None, auto_tag=False)
        assert result["edges_added"] == 0


# ── Fix E: dynamic exclusive pairs ──────────────────────────────────────────

class TestDynamicExclusivePairs:
    def test_infer_finds_static_pairs(self):
        from graphify_enhanced.contradict import _infer_exclusive_pairs
        G = _make_graph()
        pairs = _infer_exclusive_pairs(G)
        pair_set = {frozenset(p) for p in pairs}
        assert frozenset(["calls","never_calls"]) in pair_set

    def test_infer_finds_dynamic_pair(self):
        from graphify_enhanced.contradict import _infer_exclusive_pairs
        G = nx.Graph()
        G.add_node("a"); G.add_node("b"); G.add_node("c")
        G.add_edge("a","b", relation="depends_on", confidence="EXTRACTED")
        G.add_edge("a","c", relation="never_depends_on", confidence="INFERRED")
        pairs = _infer_exclusive_pairs(G)
        pair_set = {frozenset(p) for p in pairs}
        assert frozenset(["depends_on","never_depends_on"]) in pair_set

    def test_extra_exclusive_pairs_accepted(self):
        from graphify_enhanced.contradict import detect_contradictions
        # MultiGraph allows multiple edges between same pair
        G = nx.MultiGraph()
        G.add_node("a"); G.add_node("b")
        G.add_edge("a","b", relation="wraps", confidence="EXTRACTED")
        G.add_edge("a","b", relation="ignores", confidence="INFERRED")
        report = detect_contradictions(G, extra_exclusive_pairs=[("wraps","ignores")])
        assert report["total"] >= 1

    def test_exclusive_pairs_used_in_report(self):
        from graphify_enhanced.contradict import detect_contradictions
        G = _make_graph()
        report = detect_contradictions(G, annotate_graph=False)
        assert report["exclusive_pairs_used"] >= 4  # at least the static ones


# ── Fix A: directed graph awareness ─────────────────────────────────────────

class TestDirectedAwareness:
    def test_directed_graph_loads(self):
        from graphify_enhanced.pipeline import enhance_existing_graph
        G = _make_graph(directed=True)
        assert G.is_directed()

    def test_budget_directed_bfs(self):
        from graphify_enhanced.budget import extract_budgeted_subgraph
        G = _make_graph(directed=True)
        result = extract_budgeted_subgraph(G, root_ids=["auth_service"], token_budget=2000)
        assert "directed" in result

    def test_contradict_directed_edge_index(self):
        from graphify_enhanced.contradict import _build_edge_index
        G = nx.DiGraph()
        G.add_node("a"); G.add_node("b")
        G.add_edge("a","b", relation="calls", confidence="EXTRACTED")
        G.add_edge("b","a", relation="calls", confidence="INFERRED")
        index = _build_edge_index(G)
        # Directed: (a,b) and (b,a) are different pairs
        assert ("a","b") in index
        assert ("b","a") in index

    def test_causal_directed_traversal(self):
        from graphify_enhanced.causal import top_causal_chains
        G = nx.DiGraph()
        G.add_node("cve"); G.add_node("fix"); G.add_node("token")
        G.add_edge("cve","fix", relation="mandated_by", causal_edge=True, confidence="INFERRED")
        G.add_edge("fix","token", relation="required_by", causal_edge=True, confidence="INFERRED")
        chains = top_causal_chains(G)
        assert len(chains) >= 1
        assert chains[0]["root_label"] == "cve"


# ── Fix D: smart causal target selection ─────────────────────────────────────

class TestCausalTargetSelection:
    def test_interest_score_keyword_boost(self):
        from graphify_enhanced.causal import _causal_interest_score
        G = nx.Graph()
        G.add_node("a", label="security_fix_handler")
        G.add_node("b", label="base_config")
        G.add_edge("a","b", relation="uses")
        score_a = _causal_interest_score("a", G)
        score_b = _causal_interest_score("b", G)
        assert score_a > score_b

    def test_interest_score_rationale_boost(self):
        from graphify_enhanced.causal import _causal_interest_score
        G = nx.Graph()
        G.add_node("a", label="AuthService")
        G.add_node("rationale_node", label="OAuth2 reason", node_type="rationale_for")
        G.add_edge("rationale_node","a")
        score = _causal_interest_score("a", G)
        assert score >= 2.0

    def test_select_causal_targets_prefers_interesting(self):
        from graphify_enhanced.causal import _select_causal_targets
        G = nx.Graph()
        G.add_node("fix", label="security_fix")
        G.add_node("base", label="base_utils")
        G.add_node("other", label="other_thing")
        # Give all degree >= 2
        G.add_edge("fix","base"); G.add_edge("fix","other")
        G.add_edge("base","other")
        targets = _select_causal_targets(G, min_degree=1, max_targets=2)
        assert "fix" in targets


# ── Fix F: writeback idempotency + conflict detection ────────────────────────

class TestWritebackFix:
    def test_duplicate_annotation_skipped(self, tmp_path):
        from graphify_enhanced.writeback import handle_annotate_node
        G = _make_graph()
        cp = tmp_path / "c.jsonl"
        r1 = handle_annotate_node(G, "auth_service", "Same note.", cp)
        r2 = handle_annotate_node(G, "auth_service", "Same note.", cp)
        assert r1["ok"] and r2["ok"]
        assert r2.get("duplicate") is True
        # Only one record written
        records = [json.loads(l) for l in cp.read_text().splitlines() if l.strip()]
        assert len(records) == 1

    def test_edge_conflict_detected(self, tmp_path):
        from graphify_enhanced.writeback import handle_correct_edge
        G = _make_graph()
        cp = tmp_path / "c.jsonl"
        handle_correct_edge(G, "login_fn","auth_service","delegates_to", cp)
        # Different relation → should be blocked as conflict
        r = handle_correct_edge(G, "login_fn","auth_service","proxies", cp)
        assert r["ok"] is False
        assert r.get("conflict") is True

    def test_apply_corrections_idempotent(self, tmp_path):
        from graphify_enhanced.writeback import handle_annotate_node, apply_corrections
        G = _make_graph()
        cp = tmp_path / "corrections.jsonl"
        handle_annotate_node(G, "auth_service", "Note.", cp)
        nodes = [{"id": nid, **d} for nid, d in G.nodes(data=True)]
        edges = [{"source": u,"target": v,**d} for u,v,d in G.edges(data=True)]
        gp = tmp_path / "graph.json"
        gp.write_text(json.dumps({"nodes":nodes,"edges":edges}))
        r1 = apply_corrections(gp, cp)
        r2 = apply_corrections(gp, cp)  # second run should skip all
        assert r1["applied"] == 1
        assert r2["applied"] == 0
        assert r2["skipped"] == 1


# ── Fix G: health score ──────────────────────────────────────────────────────

class TestHealthScore:
    def test_health_score_structure(self):
        from graphify_enhanced.report_json import compute_health_score
        G = _make_graph()
        h = compute_health_score(G)
        assert "score" in h and "grade" in h
        assert 0 <= h["score"] <= 100
        assert h["grade"] in ("A","B","C","D","F")

    def test_health_score_in_report(self):
        from graphify_enhanced.report_json import build_report_json
        G = _make_graph()
        report = build_report_json(G)
        assert "health" in report
        assert report["health"]["score"] >= 0

    def test_health_penalises_contradictions(self):
        from graphify_enhanced.report_json import compute_health_score
        G = _make_graph()
        no_contradict = compute_health_score(G, contradiction_report={"total":0})
        many_contradict = compute_health_score(G, contradiction_report={"total":10})
        assert no_contradict["score"] > many_contradict["score"]

    def test_health_score_printed(self, capsys):
        from graphify_enhanced.report_json import build_report_json, print_stats
        G = _make_graph()
        report = build_report_json(G)
        print_stats(report)
        out = capsys.readouterr().out
        assert "Health score" in out
        assert "grade" in out.lower() or "/" in out


# ── Fix H: structured warnings ──────────────────────────────────────────────

class TestWarnings:
    def test_warnings_in_report(self):
        from graphify_enhanced.report_json import build_report_json
        G = _make_graph()
        warnings = [{"module":"temporal","op":"enrich","reason":"git not found"}]
        report = build_report_json(G, warnings=warnings)
        assert len(report["warnings"]) == 1
        assert report["warnings"][0]["module"] == "temporal"

    def test_warnings_printed(self, capsys):
        from graphify_enhanced.report_json import build_report_json, print_stats
        G = _make_graph()
        warnings = [{"module":"causal","op":"llm_call","reason":"timeout"}]
        report = build_report_json(G, warnings=warnings)
        print_stats(report)
        assert "Warnings" in capsys.readouterr().out

    def test_pipeline_collects_warnings(self, tmp_path):
        from graphify_enhanced.pipeline import enhance_existing_graph
        G = _make_graph()
        nodes = [{"id": nid,**d} for nid,d in G.nodes(data=True)]
        edges = [{"source":u,"target":v,**d} for u,v,d in G.edges(data=True)]
        gp = tmp_path/"graph.json"
        gp.write_text(json.dumps({"nodes":nodes,"edges":edges}))
        result = enhance_existing_graph(gp, corpus_root=tmp_path, verbose=False)
        assert "warnings" in result
        assert isinstance(result["warnings"], list)


# ── Fix I: graph export ──────────────────────────────────────────────────────

class TestGraphExport:
    def test_save_enhanced_graph_creates_file(self, tmp_path):
        from graphify_enhanced.report_json import save_enhanced_graph
        G = _make_graph()
        path = save_enhanced_graph(G, tmp_path)
        assert path.exists()
        data = json.loads(path.read_text())
        assert "nodes" in data and "edges" in data
        assert data["_schema"] == "graphify-enhanced-1.0"

    def test_enhanced_graph_preserves_new_attrs(self, tmp_path):
        from graphify_enhanced.report_json import save_enhanced_graph
        G = _make_graph()
        G.nodes["auth_service"]["first_seen"] = "2024-01-01T00:00:00Z"
        G.nodes["login_fn"]["contradiction"] = True
        path = save_enhanced_graph(G, tmp_path)
        data = json.loads(path.read_text())
        auth = next(n for n in data["nodes"] if n["id"] == "auth_service")
        login = next(n for n in data["nodes"] if n["id"] == "login_fn")
        assert auth["first_seen"] == "2024-01-01T00:00:00Z"
        assert login["contradiction"] is True

    def test_enhanced_graph_preserves_new_edge_types(self, tmp_path):
        from graphify_enhanced.report_json import save_enhanced_graph
        G = _make_graph()
        G.add_edge("auth_service","user_model",
                   relation="supersedes", confidence="INFERRED", temporal_edge=True)
        path = save_enhanced_graph(G, tmp_path)
        data = json.loads(path.read_text())
        supersedes = [e for e in data["edges"] if e.get("relation") == "supersedes"]
        assert len(supersedes) >= 1

    def test_pipeline_produces_enhanced_graph(self, tmp_path):
        from graphify_enhanced.pipeline import enhance_existing_graph
        G = _make_graph()
        nodes = [{"id":nid,**d} for nid,d in G.nodes(data=True)]
        edges = [{"source":u,"target":v,**d} for u,v,d in G.edges(data=True)]
        gp = tmp_path/"graph.json"
        gp.write_text(json.dumps({"nodes":nodes,"edges":edges}))
        result = enhance_existing_graph(gp, corpus_root=tmp_path, verbose=False)
        assert result.get("enhanced_graph_path") is not None
        assert Path(result["enhanced_graph_path"]).exists()

    def test_directed_flag_preserved(self, tmp_path):
        from graphify_enhanced.report_json import save_enhanced_graph
        G = nx.DiGraph()
        G.add_node("a"); G.add_node("b")
        G.add_edge("a","b", relation="calls")
        path = save_enhanced_graph(G, tmp_path)
        data = json.loads(path.read_text())
        assert data["_directed"] is True


# ── Fix B: fuzzy query matching ──────────────────────────────────────────────

class TestFuzzyQuery:
    def test_exact_substring_match(self):
        from graphify_enhanced.budget import find_root_ids_for_query
        G = _make_graph()
        roots = find_root_ids_for_query(G, "AuthService")
        assert "auth_service" in roots

    def test_partial_term_match(self):
        from graphify_enhanced.budget import find_root_ids_for_query
        G = _make_graph()
        roots = find_root_ids_for_query(G, "login auth")
        assert len(roots) > 0

    def test_hash_id_falls_back_to_fuzzy(self):
        from graphify_enhanced.budget import find_root_ids_for_query
        G = nx.Graph()
        G.add_node("sha256_abc123", label="AuthService")
        roots = find_root_ids_for_query(G, "AuthService")
        assert "sha256_abc123" in roots

    def test_no_match_returns_empty(self):
        from graphify_enhanced.budget import find_root_ids_for_query
        G = _make_graph()
        roots = find_root_ids_for_query(G, "zzzznonexistent12345xyz")
        assert roots == []

    def test_centrality_cache_invalidation(self):
        from graphify_enhanced.budget import _degree_centrality, invalidate_centrality_cache, _centrality_cache
        G = _make_graph()
        invalidate_centrality_cache()
        c1 = _degree_centrality(G)
        G.add_node("new_node", label="NewNode")
        G.add_edge("new_node","auth_service")
        invalidate_centrality_cache()
        c2 = _degree_centrality(G)
        assert c1 != c2  # cache properly invalidated
