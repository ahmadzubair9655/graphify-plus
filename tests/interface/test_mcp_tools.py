"""MCP tool function tests — exercise tool implementations directly,
without spinning up the stdio server (which needs the optional ``mcp``
extra)."""

from __future__ import annotations

import shutil
from pathlib import Path

from graphify_plus.core import cas
from graphify_plus.core.ingest import ingest
from graphify_plus.core.skeletonizer import skeletonize_all
from graphify_plus.interface.mcp_server import (
    TOOLS,
    manifest,
    tool_context,
    tool_find,
    tool_skeleton,
)
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


def test_manifest_lists_expected_tools():
    m = manifest()
    names = {t["name"] for t in m["tools"]}
    assert {
        "gp_init",
        "gp_context",
        "gp_skeleton",
        "gp_find",
        "gp_simulate",
        "gp_guardrails",
        "gp_blast_radius",
    } <= names


def test_tool_skeleton_by_target(tmp_path: Path):
    repo = _seed(tmp_path)
    out = tool_skeleton({"repo": str(repo), "target": "Invoice.total"})
    assert "def total" in out["text"]


def test_tool_find_returns_relevant(tmp_path: Path):
    repo = _seed(tmp_path)
    out = tool_find({"repo": str(repo), "intent": "where is invoice total computed"})
    qnames = [m["qualified_name"] for m in out["matches"]]
    assert qnames[0] == "invoice.Invoice.total"


def test_tool_context_returns_text_and_tokens(tmp_path: Path):
    repo = _seed(tmp_path)
    out = tool_context({"repo": str(repo), "target": "Invoice.total", "max_tokens": 1500})
    assert out["text"]
    assert out["tokens"] > 0
    assert isinstance(out["manifest_hashes"], list)


def test_all_tools_are_callable():
    for name, fn in TOOLS.items():
        assert callable(fn), f"tool {name} is not callable"
