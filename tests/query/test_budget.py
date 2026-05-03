from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from graphify_plus.core import cas
from graphify_plus.core.adapters import PythonAdapter
from graphify_plus.core.ingest import ingest
from graphify_plus.core.skeletonizer import skeletonize_all
from graphify_plus.core.symbol_graph import build as build_graph
from graphify_plus.query.budget import count_tokens, frame_to_budget
from graphify_plus.query.partition import compute_partition
from graphify_plus.runtime.store import Store, cache_path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


def _seeded_repo(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    shutil.copytree(FIXTURE, target)
    res = ingest(target, parallel=False)
    skeletons = skeletonize_all(res.symbols)
    s = Store(cache_path(target))
    try:
        s.replace_all(res.symbols, res.edges)
        for sid, body in skeletons.items():
            h = cas.put(s, body)
            s.link_skeleton(sid, h)
    finally:
        s.close()
    return target


def test_budget_respects_max_tokens(tmp_path: Path):
    repo = _seeded_repo(tmp_path)
    s = Store(cache_path(repo))
    try:
        symbols = s.all_symbols()
        edges = s.all_edges()
        G = build_graph(symbols, edges)
        # Pick a path between two real nodes.
        invoice_total = next(x for x in symbols if x["qualified_name"] == "invoice.Invoice.total")
        invoice_mod = next(x for x in symbols if x["qualified_name"] == "invoice")
        partition = compute_partition(G, invoice_mod["id"], invoice_total["id"])
        framed = frame_to_budget(partition, s, max_tokens=200)
        # Tokens stay within budget plus a 5% slack for the cut marker.
        assert framed.tokens <= int(200 * 1.05) + 5
        assert "@python" in framed.text
    finally:
        s.close()


@settings(max_examples=20, deadline=None)
@given(budget=st.integers(min_value=50, max_value=2000))
def test_property_budget_is_bounded(budget, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("budget_prop")
    # Synthesise a tiny graph with one path.
    src = tmp_path / "m.py"
    src.write_text(
        "def root():\n    return middle()\n\n"
        "def middle():\n    return leaf()\n\n"
        "def leaf():\n    return 1\n"
    )
    res = ingest(tmp_path, parallel=False)
    skeletons = skeletonize_all(res.symbols)
    s = Store(cache_path(tmp_path))
    try:
        s.replace_all(res.symbols, res.edges)
        for sid, body in skeletons.items():
            h = cas.put(s, body)
            s.link_skeleton(sid, h)
        symbols = s.all_symbols()
        edges = s.all_edges()
        G = build_graph(symbols, edges)
        root = next(x for x in symbols if x["qualified_name"] == "m.root")
        leaf = next(x for x in symbols if x["qualified_name"] == "m.leaf")
        partition = compute_partition(G, root["id"], leaf["id"])
        framed = frame_to_budget(partition, s, max_tokens=budget)
        # Reported tokens align with re-tokenising the actual text.
        assert framed.tokens >= count_tokens(framed.text) - 5
        assert framed.tokens <= int(budget * 1.05) + count_tokens(
            "« 1 additional nodes cut by budget »"
        )
    finally:
        s.close()


# ---- CLI smoke -----------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "graphify_plus", *args],
        capture_output=True,
        check=False,
    )


def test_context_cli_smoke(tmp_path: Path):
    target = tmp_path / "repo"
    shutil.copytree(FIXTURE, target)
    init = _run("init", "--repo", str(target), "--no-parallel")
    assert init.returncode == 0, init.stderr.decode()
    out = _run(
        "context",
        "--repo",
        str(target),
        "--target",
        "Invoice.total",
        "--max-tokens",
        "1500",
    )
    assert out.returncode == 0, out.stderr.decode()
    text = out.stdout.decode()
    assert "Invoice" in text
    assert "@python" in text


# Trivial use of PythonAdapter so the import isn't unused; ensures the
# fixture remains parseable independently of the CLI test path.
def test_python_adapter_still_parses_fixture(tmp_path: Path):
    src = (FIXTURE / "backend" / "billing" / "invoice.py").read_bytes()
    syms, _ = PythonAdapter().parse(Path("invoice.py"), src)
    assert any(s["qualified_name"].endswith("Invoice.total") for s in syms)
