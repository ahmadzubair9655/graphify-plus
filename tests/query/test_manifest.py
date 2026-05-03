from __future__ import annotations

import re
import shutil
from pathlib import Path

from graphify_plus.core import cas
from graphify_plus.core.ingest import ingest
from graphify_plus.core.skeletonizer import skeletonize_all
from graphify_plus.core.symbol_graph import build as build_graph
from graphify_plus.query.manifest import generate, render
from graphify_plus.query.sync_docs import (
    SECTION_HEADERS,
    mark_completed,
    merge_into_existing,
    regenerate,
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


def test_manifest_dependency_order_skips_externals(tmp_path: Path):
    repo = _seed(tmp_path)
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        m = generate("Add invoice line-item discount field", G, s)
    finally:
        s.close()
    assert m.dependency_order, "expected non-empty plan"
    # No external (unresolved imports / builtins) should appear.
    for s_ in m.dependency_order:
        assert s_.kind != "external"
    qnames = [s_.qualified_name for s_ in m.dependency_order]
    assert "invoice.Invoice" in qnames
    assert "invoice.Invoice.total" in qnames


def test_render_is_deterministic(tmp_path: Path):
    repo = _seed(tmp_path)
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        m = generate("Refactor invoice", G, s)
    finally:
        s.close()
    a = render(m)
    b = render(m)
    assert a == b
    assert a.startswith("# STAGING_PLAN")
    assert "## Dependency order" in a
    assert "## Validation checkpoints" in a


def test_regenerate_writes_into_target_repo(tmp_path: Path):
    repo = _seed(tmp_path)
    plan_path = regenerate(repo, "Add invoice line-item discount field")
    # Plan must land in the TARGET repo, never inside graphify-plus itself.
    assert plan_path == repo / "STAGING_PLAN.md"
    assert plan_path.exists()
    text = plan_path.read_text()
    assert "Add invoice line-item discount field" in text


def test_merge_preserves_human_edits():
    existing = (
        "# STAGING_PLAN\n\n"
        "_Task:_ old\n\n"
        "## Notes\n- old note\n\n"
        "## My private playbook\n"
        "- step A\n- step B\n\n"
        "## Dependency order\n"
        "| # | kind | qualified name | path | impact |\n"
        "|---|------|----------------|------|--------|\n"
        "| 1 | `class` | `Old` | `old.py` | 0.0 |\n\n"
        "## Validation checkpoints\n- [symbol_exists] old\n"
    )
    regenerated = (
        "# STAGING_PLAN\n\n_Task:_ new\n\n"
        "## Notes\n- new note\n\n"
        "## Dependency order\n"
        "| # | kind | qualified name | path | impact |\n"
        "|---|------|----------------|------|--------|\n"
        "| 1 | `class` | `New` | `new.py` | 0.0 |\n\n"
        "## Validation checkpoints\n- [symbol_exists] new\n"
    )
    out = merge_into_existing(existing, regenerated)
    # Auto-sections updated...
    assert "`New`" in out
    assert "[symbol_exists] new" in out
    # ...human-authored section preserved verbatim.
    assert "## My private playbook" in out
    assert "step A" in out and "step B" in out
    # And every standard header still present.
    for h in SECTION_HEADERS:
        assert h in out


def test_mark_completed_appends_check(tmp_path: Path):
    plan = tmp_path / "STAGING_PLAN.md"
    plan.write_text(
        "## Dependency order\n\n"
        "| # | kind | qualified name | path | impact |\n"
        "|---|------|----------------|------|--------|\n"
        "| 1 | `class` | `Foo` | `src/foo.py` | 0.5 |\n"
        "| 2 | `class` | `Bar` | `src/bar.py` | 0.5 |\n"
    )
    mark_completed(plan, {"src/foo.py"})
    out = plan.read_text()
    foo_row = next(line for line in out.splitlines() if "Foo" in line)
    bar_row = next(line for line in out.splitlines() if "Bar" in line)
    assert "✓" in foo_row
    assert "✓" not in bar_row
    # Idempotent: re-running doesn't double-tick.
    mark_completed(plan, {"src/foo.py"})
    out2 = plan.read_text()
    foo_row2 = next(line for line in out2.splitlines() if "Foo" in line)
    assert len(re.findall("✓", foo_row2)) == 1
