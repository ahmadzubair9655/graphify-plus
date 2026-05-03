from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from graphify_plus.core import cas
from graphify_plus.core.ingest import ingest
from graphify_plus.core.skeletonizer import skeletonize_all
from graphify_plus.core.symbol_graph import build as build_graph
from graphify_plus.query.stitch import discover_specs, parse_spec, stitch_into
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


def test_discover_specs_finds_openapi(tmp_path: Path):
    repo = _seed(tmp_path)
    specs = discover_specs(repo)
    assert any(p.name == "openapi.yaml" for p in specs)


def test_parse_spec_extracts_endpoints(tmp_path: Path):
    repo = _seed(tmp_path)
    spec = next(p for p in discover_specs(repo) if p.name == "openapi.yaml")
    eps = parse_spec(spec)
    assert any(e.path == "/api/invoice" and e.method == "POST" for e in eps)


def test_stitch_creates_endpoint_and_cross_edge(tmp_path: Path):
    repo = _seed(tmp_path)
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        result = stitch_into(G, [repo])
    finally:
        s.close()
    # 1 endpoint expected from openapi.yaml.
    assert len(result.endpoints) >= 1
    # The frontend api.ts has `/api/invoice` literal → cross edge to endpoint.
    assert any(e.op_id == "postInvoice" for e in result.edges)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "graphify_plus", *args],
        capture_output=True,
        check=False,
    )


_RULES = """
layers:
  ui: ["frontend/**"]
  api: ["backend/api*"]
  database: ["backend/billing/**"]
  service: ["service/**"]
"""


def test_coordinate_writes_agent_plans_into_target_repo(tmp_path: Path):
    repo = _seed(tmp_path)
    (repo / ".graphify_plus" / "rules.yaml").write_text(_RULES)
    out = _run("coordinate", "--repo", str(repo), "--task", "T")
    assert out.returncode == 0, out.stderr.decode()
    expected = ["UI", "API", "DATABASE", "SERVICE"]
    for name in expected:
        plan = repo / f"STAGING_PLAN_{name}.md"
        assert plan.exists(), f"missing {plan}"
        text = plan.read_text()
        assert "STAGING_PLAN" in text
        assert f"agent={name.lower()}" in text


def test_coordinate_records_upstream_dependencies(tmp_path: Path):
    repo = _seed(tmp_path)
    (repo / ".graphify_plus" / "rules.yaml").write_text(_RULES)
    out = _run("coordinate", "--repo", str(repo), "--task", "T")
    assert out.returncode == 0
    db_plan = (repo / "STAGING_PLAN_DATABASE.md").read_text()
    # The UI has a frontend → backend api crosses_to edge so UI depends
    # on at least one upstream agent. DB has no upstream in this fixture.
    assert "no upstream" in db_plan
