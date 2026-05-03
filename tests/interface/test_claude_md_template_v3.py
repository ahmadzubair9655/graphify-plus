"""Template v3 emits the verbatim confidence-warning paragraph and uses `gp` aliases."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CONFIDENCE_PARAGRAPH = (
    "When confidence < 0.7 on any edge in your context output, verify by "
    "reading the source file directly before relying on it. Low-confidence "
    "edges are heuristic guesses, not facts."
)


def test_template_version_marker_is_3_and_uses_gp_alias(tmp_path: Path):
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
    assert "<!-- graphify-plus-template-version: 3 -->" in text
    assert CONFIDENCE_PARAGRAPH in text
    # v3: workflow steps use the `gp` alias and audit points at the JSONL artefact.
    assert "`gp plan " in text
    assert "`gp context " in text
    assert "`gp audit .graphify_plus/graph_symbols.jsonl`" in text
    assert "gp enrich git" in text
    # `graphify-plus <subcommand>` should no longer appear in the workflow section.
    assert "graphify-plus init" not in text
    assert "graphify-plus plan" not in text
    assert "graphify-plus audit" not in text
