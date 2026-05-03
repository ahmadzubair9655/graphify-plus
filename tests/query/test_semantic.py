from __future__ import annotations

import shutil
from pathlib import Path

from graphify_plus.core import cas
from graphify_plus.core.ingest import ingest
from graphify_plus.core.skeletonizer import skeletonize_all
from graphify_plus.core.symbol_graph import build as build_graph
from graphify_plus.query.semantic import communities, find
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


def test_find_locates_invoice_total(tmp_path: Path):
    repo = _seed(tmp_path)
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        matches = find(s, G, "where is invoice total computed", top_k=5)
    finally:
        s.close()
    assert matches, "expected at least one match"
    qnames = [m.symbol["qualified_name"] for m in matches]
    # Top match must be the relevant symbol.
    assert qnames[0] == "invoice.Invoice.total"


def test_communities_are_deterministic(tmp_path: Path):
    repo = _seed(tmp_path)
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        c1 = communities(s, G)
        # Re-build to bypass meta cache: clear it then rebuild.
        s.set_meta("communities_v1", "")
        s.conn.execute("DELETE FROM meta WHERE key='communities_v1'")
        c2 = communities(s, G)
    finally:
        s.close()
    assert c1 == c2


def test_no_matches_for_irrelevant_query(tmp_path: Path):
    repo = _seed(tmp_path)
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        # Term that doesn't appear in any symbol; BM25 returns score≤0,
        # which find() filters out.
        matches = find(s, G, "zxqvy nonexistent unicornium", top_k=5)
    finally:
        s.close()
    assert matches == []
