from __future__ import annotations

from pathlib import Path

import networkx as nx

from graphify_plus.interface.visual import render_cluster


def _G() -> nx.MultiDiGraph:
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    G.add_node(
        "ui1",
        id="ui1",
        kind="function",
        name="Button",
        qualified_name="ui.Button",
        path="frontend/button.tsx",
    )
    G.add_node(
        "api1",
        id="api1",
        kind="function",
        name="post",
        qualified_name="api.post",
        path="backend/api/handler.py",
    )
    G.add_node(
        "db1",
        id="db1",
        kind="class",
        name="Repo",
        qualified_name="db.Repo",
        path="backend/db/repo.py",
    )
    G.add_edge("ui1", "api1", kind="calls")
    G.add_edge("api1", "db1", kind="imports")
    return G


_LAYERS = {
    "ui": ["frontend/**"],
    "api": ["backend/api/**"],
    "database": ["backend/db/**"],
}


def test_render_writes_dot(tmp_path: Path):
    G = _G()
    result = render_cluster(
        G,
        list(G.nodes),
        tmp_path,
        cluster_id=7,
        layers=_LAYERS,
        gatekeepers={"api1"},
    )
    assert result.dot_path.name == "cluster_7.dot"
    text = result.dot_path.read_text()
    assert "digraph G" in text
    # Layer colour assigned.
    assert 'fillcolor="#4f8ed8"' in text  # ui blue
    assert 'fillcolor="#3aa55d"' in text  # api green
    # Gatekeeper has thicker border.
    assert 'penwidth="3"' in text


def test_render_is_byte_stable(tmp_path: Path):
    G = _G()
    a = render_cluster(
        G, list(G.nodes), tmp_path / "a", cluster_id=0, layers=_LAYERS, gatekeepers=set()
    )
    b = render_cluster(
        G, list(G.nodes), tmp_path / "b", cluster_id=0, layers=_LAYERS, gatekeepers=set()
    )
    assert a.dot_path.read_text() == b.dot_path.read_text()


def test_kind_to_shape_mapping(tmp_path: Path):
    G = _G()
    result = render_cluster(
        G, list(G.nodes), tmp_path, cluster_id=0, layers=_LAYERS, gatekeepers=set()
    )
    text = result.dot_path.read_text()
    assert 'shape="box"' in text  # function
    assert 'shape="ellipse"' in text  # class
