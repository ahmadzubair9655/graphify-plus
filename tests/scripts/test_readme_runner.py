"""Sanity check that scripts/test_readme.py extracts and runs blocks."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "test_readme.py"


def _load():
    spec = importlib.util.spec_from_file_location("test_readme", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_extract_blocks_finds_bash_fences_and_skips_marked():
    m = _load()
    text = (
        "intro\n"
        "```bash\necho hi\n```\n"
        "more\n"
        "<!-- doctest:skip -->\n"
        "```bash\necho skipped\n```\n"
        "```sh\necho sh\n```\n"
    )
    blocks = m.extract_blocks(text)
    bodies = [b for _, b in blocks]
    assert "echo hi" in bodies
    assert "echo sh" in bodies
    assert "echo skipped" not in bodies


def test_run_block_executes_in_cwd(tmp_path):
    m = _load()
    rc, out, _err = m.run_block("pwd", tmp_path)
    assert rc == 0
    assert str(tmp_path.resolve()) in out
