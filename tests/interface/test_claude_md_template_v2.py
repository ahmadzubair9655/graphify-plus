"""Template v2 emits the verbatim confidence-warning paragraph."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CONFIDENCE_PARAGRAPH = (
    "When confidence < 0.7 on any edge in your context output, verify by "
    "reading the source file directly before relying on it. Low-confidence "
    "edges are heuristic guesses, not facts."
)


def test_template_version_marker_is_2_and_warning_present(tmp_path: Path):
    repo = tmp_path / "sample"
    repo.mkdir()
    (repo / "a.py").write_text("def f(): pass\n")
    subprocess.run(
        [sys.executable, "-m", "graphify_plus", "init", "--repo", str(repo), "--no-parallel"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "graphify_plus", "claude-md", "--repo", str(repo)],
        check=True,
        capture_output=True,
    )
    text = (repo / "CLAUDE.md").read_text()
    assert "<!-- graphify-plus-template-version: 2 -->" in text
    assert CONFIDENCE_PARAGRAPH in text
