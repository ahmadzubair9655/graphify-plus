from __future__ import annotations

from pathlib import Path

import networkx as nx

from graphify_plus.query.tests_gen import for_subgraph, write_scaffolds


def _G() -> nx.MultiDiGraph:
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    G.add_node("a", id="a", qualified_name="mod.a", path="src/mod.py", language="python")
    G.add_node("b", id="b", qualified_name="mod.b", path="src/mod.py", language="python")
    G.add_node("c", id="c", qualified_name="ui.x", path="frontend/x.ts", language="typescript")
    G.add_edge("a", "b", kind="calls")
    G.add_edge("c", "a", kind="imports")
    G.add_edge("a", "b", kind="contains")  # filtered out
    return G


def test_for_subgraph_skips_structural_edges():
    sc = for_subgraph(_G())
    kinds = {s.edge_kind for s in sc}
    assert "calls" in kinds
    assert "imports" in kinds
    assert "contains" not in kinds


def test_language_routing():
    sc = for_subgraph(_G())
    by_kind = {(s.edge_kind, s.src_qname): s.language for s in sc}
    assert by_kind[("calls", "mod.a")] == "python"
    assert by_kind[("imports", "ui.x")] == "typescript"


def test_python_scaffold_body_is_valid_module():
    sc = next(s for s in for_subgraph(_G()) if s.language == "python")
    assert "def test_" in sc.body
    assert "raise AssertionError" in sc.body


def test_target_paths_filter():
    sc = for_subgraph(_G(), target_paths=["frontend/x.ts"])
    paths_seen = {(s.src_qname, s.dst_qname) for s in sc}
    # Every yielded scaffold must touch at least one of the targeted files.
    assert all("ui.x" in (a, b) or any(p == "frontend/x.ts" for p in (a, b)) for a, b in paths_seen)


def test_write_scaffolds_filenames(tmp_path: Path):
    sc = for_subgraph(_G())
    paths = write_scaffolds(sc, tmp_path)
    names = {p.name for p in paths}
    assert any(n.startswith("test_") and n.endswith(".py") for n in names)
    assert any(n.endswith(".test.ts") for n in names)
