from __future__ import annotations

import shutil
from pathlib import Path

from graphify_plus.core import cas
from graphify_plus.core.ingest import ingest
from graphify_plus.core.skeletonizer import skeletonize_all
from graphify_plus.interface.cli.claude_md_cmd import (
    END_MARKER,
    START_MARKER,
    amend,
    render_section,
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


def test_render_section_contains_markers_and_stack(tmp_path: Path):
    repo = _seed(tmp_path)
    section = render_section(repo)
    assert section.startswith(START_MARKER)
    assert END_MARKER in section
    assert "## Graphify-Plus" in section
    assert "python" in section.lower()
    # Tool surface listed.
    assert "gp_init" in section


def test_render_section_is_idempotent(tmp_path: Path):
    repo = _seed(tmp_path)
    a = render_section(repo)
    b = render_section(repo)
    assert a == b


def test_amend_preserves_human_authored_content(tmp_path: Path):
    repo = _seed(tmp_path)
    section = render_section(repo)
    existing = (
        "# My project's CLAUDE.md\n\n"
        "## House rules\n"
        "- Do not break the build.\n"
        "- Be kind in commit messages.\n\n"
        f"{START_MARKER}\nold content\n{END_MARKER}\n\n"
        "## Other notes\n"
        "Random content here.\n"
    )
    out = amend(existing, section)
    assert "## House rules" in out
    assert "Do not break the build." in out
    assert "## Other notes" in out
    assert "Random content here." in out
    # Old graphify-plus block replaced, not duplicated.
    assert out.count(START_MARKER) == 1
    assert out.count(END_MARKER) == 1
    assert "old content" not in out


def test_amend_creates_when_no_existing():
    section = f"{START_MARKER}\n## Graphify-Plus\n\nbody\n{END_MARKER}\n"
    out = amend("", section)
    assert out == section


def test_amend_appends_when_no_markers():
    section = f"{START_MARKER}\nbody\n{END_MARKER}\n"
    existing = "# Hello\n\nThe project.\n"
    out = amend(existing, section)
    assert "The project." in out
    assert START_MARKER in out
