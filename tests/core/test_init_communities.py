"""Per-node community attribute writing during gp init."""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
from click.testing import CliRunner

from graphify_plus.audit.probe import (
    probe_confidence_drift,
    probe_edge_deletion_stability,
    probe_modularity_quality,
)
from graphify_plus.interface.cli.init_cmd import _assign_communities, init_cmd

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


def test_assign_communities_writes_attribute_to_every_symbol():
    symbols = [
        {"id": "a", "kind": "module", "qualified_name": "a"},
        {"id": "b", "kind": "function", "qualified_name": "a.b"},
        {"id": "c", "kind": "function", "qualified_name": "a.c"},
    ]
    edges = [
        {"src": "a", "dst": "b", "kind": "contains", "resolved": True, "confidence": 1.0},
        {"src": "b", "dst": "c", "kind": "calls", "resolved": True, "confidence": 1.0},
    ]
    comm = _assign_communities(symbols, edges)
    assert comm  # non-empty
    for s in symbols:
        assert "community" in s, f"{s['id']} missing community"
        assert isinstance(s["community"], int)


def test_init_writes_community_to_every_node_in_jsonl(tmp_path: Path):
    """End-to-end: gp init on a fixture writes per-node community to JSONL."""
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    # Copy a tiny subset to keep the test fast.
    (repo / "a.py").write_text(
        "def alpha(): pass\n"
        "def beta(): alpha()\n"
        "def gamma(): beta()\n"
    )
    result = runner.invoke(init_cmd, ["--repo", str(repo), "--no-parallel"])
    assert result.exit_code == 0, result.output

    jsonl = repo / ".graphify_plus" / "graph_symbols.jsonl"
    assert jsonl.exists()
    symbols_seen = []
    for line in jsonl.read_text().splitlines():
        rec = json.loads(line)
        if "_meta" in rec:
            continue
        if "id" in rec and "src" not in rec:
            symbols_seen.append(rec)
    assert symbols_seen
    for s in symbols_seen:
        assert "community" in s, f"symbol {s.get('qualified_name')} missing community"


def test_audit_probes_run_when_community_present():
    """Probes 1, 2, 6 must not skip when community + INFERRED edges are
    present. Constructs a minimal graph that satisfies the probes' inputs."""
    G: nx.Graph = nx.Graph()
    # Two-community graph: 0 = {n0..n4}, 1 = {n5..n9}
    for i in range(5):
        G.add_node(f"a{i}", community=0)
        G.add_node(f"b{i}", community=1)
    for i in range(4):
        G.add_edge(f"a{i}", f"a{i + 1}")
        G.add_edge(f"b{i}", f"b{i + 1}")
    G.add_edge("a0", "b0")  # one cross-community edge

    r1 = probe_edge_deletion_stability(G, iterations=2)
    assert r1["skipped"] is False, r1.get("reason")

    r6 = probe_modularity_quality(G)
    assert r6["skipped"] is False, r6.get("reason")

    # Confidence drift needs ≥3 INFERRED edges with confidence_score.
    G2: nx.Graph = nx.Graph()
    for i in range(4):
        G2.add_node(str(i), community=0)
    G2.add_edge("0", "1", confidence="INFERRED", confidence_score=0.7)
    G2.add_edge("1", "2", confidence="INFERRED", confidence_score=0.5)
    G2.add_edge("2", "3", confidence="INFERRED", confidence_score=0.6)
    r2 = probe_confidence_drift(G2)
    assert r2["skipped"] is False, r2.get("reason")


def test_modularity_runs_with_partial_community_coverage():
    """Real audit graphs include placeholder nodes (unresolved import
    targets) that never receive a community assignment. Before Fix 2 the
    modularity probe skipped with `modularity error: ...` because
    NetworkX rejected the incomplete partition. The fix backfills missing
    nodes into a synthetic community so the probe can grade.
    """
    G: nx.Graph = nx.Graph()
    # Two real communities…
    for i in range(5):
        G.add_node(f"a{i}", community=0)
        G.add_node(f"b{i}", community=1)
    for i in range(4):
        G.add_edge(f"a{i}", f"a{i + 1}")
        G.add_edge(f"b{i}", f"b{i + 1}")
    G.add_edge("a0", "b0")
    # …plus three placeholder nodes with NO community attribute.
    for ext in ("ext1", "ext2", "ext3"):
        G.add_node(ext)
        G.add_edge("a0", ext)

    r = probe_modularity_quality(G)
    assert r["skipped"] is False, r.get("reason")
    # Backfill assigned the placeholders to the synthetic -1 community.
    assert all("community" in G.nodes[n] for n in ("ext1", "ext2", "ext3"))
    assert G.nodes["ext1"]["community"] == -1


def test_audit_falls_back_when_community_missing_on_old_caches():
    """Backwards-compat path (Task B option a): graphs ingested before the
    per-node community fix lack the attribute. Audit must derive Louvain
    on-the-fly so probes still grade rather than skipping silently.
    """
    G: nx.Graph = nx.Graph()
    for i in range(5):
        G.add_node(f"a{i}")
        G.add_node(f"b{i}")
    for i in range(4):
        G.add_edge(f"a{i}", f"a{i + 1}")
        G.add_edge(f"b{i}", f"b{i + 1}")
    G.add_edge("a0", "b0")

    r = probe_modularity_quality(G)
    assert r["skipped"] is False, r.get("reason")
    # Probe should have written communities back onto nodes.
    assert all("community" in G.nodes[n] for n in G.nodes)
