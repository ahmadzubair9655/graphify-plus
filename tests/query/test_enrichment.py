"""Phase 8 enrichment tests: git, telemetry, lockfile, blast-radius."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import networkx as nx

from graphify_plus.core import cas
from graphify_plus.core.ingest import ingest
from graphify_plus.core.skeletonizer import skeletonize_all
from graphify_plus.core.symbol_graph import build as build_graph
from graphify_plus.query.blast_radius import trace as blast_trace
from graphify_plus.query.git_silo import enrich_graph, parse_log
from graphify_plus.query.lockfile import (
    attach_to_graph,
    parse_npm,
    parse_poetry_lock,
    parse_requirements_txt,
)
from graphify_plus.query.telemetry import aggregate
from graphify_plus.query.telemetry import attach_to_graph as attach_telemetry
from graphify_plus.runtime.store import Store, cache_path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


def _seed(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    shutil.copytree(FIXTURE, target)
    res = ingest(target, parallel=False)
    skel = skeletonize_all(res.symbols)
    s = Store(cache_path(target))
    try:
        s.replace_all(res.symbols, res.edges)
        for sid, body in skel.items():
            h = cas.put(s, body)
            s.link_skeleton(sid, h)
    finally:
        s.close()
    return target


# ---- git_silo ------------------------------------------------------------


def test_parse_log_extracts_per_file_counts():
    sample = (
        "GP_COMMIT abc123\t2026-01-01T10:00:00+00:00\tAlice\n"
        "1\t0\tsrc/a.py\n"
        "2\t0\tsrc/b.py\n"
        "GP_COMMIT def456\t2026-02-01T10:00:00+00:00\tBob\n"
        "5\t1\tsrc/a.py\n"
    )
    stats = parse_log(sample)
    assert stats["src/a.py"].commit_count == 2
    assert stats["src/b.py"].commit_count == 1
    assert stats["src/a.py"].last_touch_unix > stats["src/b.py"].last_touch_unix


def test_enrich_graph_attaches_volatility_and_age():
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    G.add_node("a", path="src/a.py", kind="function", in_degree=10)
    G.add_node("b", path="src/b.py", kind="function")
    sample = (
        "GP_COMMIT 1\t2026-01-01T10:00:00+00:00\tA\n"
        "1\t0\tsrc/a.py\n"
        "GP_COMMIT 2\t2026-02-01T10:00:00+00:00\tA\n"
        "1\t0\tsrc/a.py\n"
        "GP_COMMIT 3\t2026-03-01T10:00:00+00:00\tA\n"
        "1\t0\tsrc/a.py\n"
        "GP_COMMIT 4\t2026-01-01T10:00:00+00:00\tA\n"
        "1\t0\tsrc/b.py\n"
    )
    stats = parse_log(sample)
    summary = enrich_graph(G, stats)
    assert summary["files_enriched"] == 2
    assert "volatility" in G.nodes["a"]
    assert G.nodes["a"]["volatility"] >= G.nodes["b"]["volatility"]


# ---- telemetry -----------------------------------------------------------


def test_telemetry_aggregate_redacts_attrs():
    lines = [
        json.dumps(
            {
                "name": "invoice.Invoice.total",
                "attributes": {"user_email": "alice@example.com", "tax_rate": 0.2},
            }
        ),
        json.dumps(
            {
                "name": "invoice.Invoice.total",
                "attributes": {"user_email": "bob@example.com", "tax_rate": None},
            }
        ),
    ]
    shapes = aggregate(lines)
    assert "invoice.Invoice.total" in shapes
    sh = shapes["invoice.Invoice.total"]
    assert sh.sample_count == 2
    assert sh.keys["user_email"]["samples"] == 2
    # Privacy filter ran: every user_email value would have been redacted
    # to <EMAIL> before being type-classified.
    types = sh.keys["user_email"]["types"]
    assert "string" in types


def test_telemetry_attach_to_graph(tmp_path: Path):
    repo = _seed(tmp_path)
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        shapes = aggregate(
            [
                json.dumps(
                    {
                        "name": "invoice.Invoice.total",
                        "attributes": {"tax_rate": 0.2},
                    }
                )
            ]
        )
        matched = attach_telemetry(G, shapes)
    finally:
        s.close()
    assert matched >= 1
    n = next(
        attrs
        for _, attrs in G.nodes(data=True)
        if attrs.get("qualified_name") == "invoice.Invoice.total"
    )
    assert "data_shape" in n


# ---- lockfile ------------------------------------------------------------


def test_parse_requirements_txt(tmp_path: Path):
    p = tmp_path / "requirements.txt"
    p.write_text("fastapi==0.110.0\npydantic>=2.6\n# comment\nrich\n")
    deps = parse_requirements_txt(p)
    names = {d.package for d in deps}
    assert {"fastapi", "pydantic", "rich"} <= names


def test_parse_npm(tmp_path: Path):
    p = tmp_path / "package-lock.json"
    p.write_text(
        json.dumps(
            {
                "packages": {
                    "": {"name": "myapp"},
                    "node_modules/react": {"version": "18.2.0"},
                    "node_modules/@types/node": {"version": "20.0.0"},
                }
            }
        )
    )
    deps = parse_npm(p)
    names = {d.package for d in deps}
    assert "react" in names
    assert "@types/node" in names


def test_parse_poetry_lock(tmp_path: Path):
    p = tmp_path / "poetry.lock"
    p.write_text(
        '[[package]]\nname = "fastapi"\nversion = "0.110.0"\n'
        '[[package]]\nname = "pydantic"\nversion = "2.6"\n'
    )
    deps = parse_poetry_lock(p)
    names = {d.package for d in deps}
    assert "fastapi" in names and "pydantic" in names


def test_lockfile_attaches_dependency_nodes(tmp_path: Path):
    repo = _seed(tmp_path)
    (repo / "requirements.txt").write_text("fastapi==0.110.0\nlodash==4.17.21\n")
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        summary = attach_to_graph(repo, G)
    finally:
        s.close()
    assert summary["packages"] == 2
    deps = [a for _, a in G.nodes(data=True) if a.get("kind") == "dependency"]
    assert {d["name"] for d in deps} == {"fastapi", "lodash"}


# ---- blast-radius --------------------------------------------------------


def test_blast_radius_unknown_package_returns_empty(tmp_path: Path):
    repo = _seed(tmp_path)
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        report = blast_trace(G, "nonexistent")
    finally:
        s.close()
    assert report.hits == []
    assert "No dependency node" in report.suggestion


def test_blast_radius_traces_in_repo_dependents(tmp_path: Path):
    repo = _seed(tmp_path)
    (repo / "requirements.txt").write_text("fastapi==0.110.0\n")
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        attach_to_graph(repo, G)
        # Synthesise a transitive 'imports' edge: a real symbol → fastapi dep.
        from graphify_plus.core.adapters import make_symbol_id

        fastapi_id = make_symbol_id("dep::pypi", "fastapi")
        sym = next(x for x in s.all_symbols() if x["qualified_name"] == "invoice.Invoice")
        G.add_edge(sym["id"], fastapi_id, kind="imports")
        report = blast_trace(G, "fastapi")
    finally:
        s.close()
    assert any(h.qualified_name == "invoice.Invoice" for h in report.hits)


# ---- repo-aware enrich CLI smoke ----------------------------------------


def test_enrich_lockfile_persists_via_cli(tmp_path: Path):
    repo = _seed(tmp_path)
    (repo / "requirements.txt").write_text("fastapi==0.110.0\n")
    res = subprocess.run(
        ["python", "-m", "graphify_plus", "enrich", "--repo", str(repo), "lockfile"],
        capture_output=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr.decode()
    s = Store(cache_path(repo))
    try:
        kinds = {
            a.get("kind") for _, a in build_graph(s.all_symbols(), s.all_edges()).nodes(data=True)
        }
    finally:
        s.close()
    assert "dependency" in kinds
