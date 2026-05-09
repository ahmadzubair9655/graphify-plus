"""Tests for Layer 21 (schema versioning) and Layer 23 (CLAUDE.md owned section)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from graphify_plus.daemon.context_file import (
    build_section,
    render_section,
    strip_owned_section,
    update_all_known,
    update_file,
)
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.schema_version import (
    COMPATIBILITY_WINDOW,
    DAEMON_SCHEMA_VERSION,
    SchemaTooNew,
    SchemaTooOld,
    check,
    load_artifact,
    migrate,
    register_migration,
    save_artifact,
    stamp,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd

# ---- Layer 21 ------------------------------------------------------------


def test_stamp_adds_version() -> None:
    assert stamp({"a": 1})["_schema_version"] == DAEMON_SCHEMA_VERSION


def test_check_passes_current_version() -> None:
    assert check({"_schema_version": DAEMON_SCHEMA_VERSION}) == DAEMON_SCHEMA_VERSION


def test_check_too_new_raises() -> None:
    with pytest.raises(SchemaTooNew):
        check({"_schema_version": DAEMON_SCHEMA_VERSION + 1})


def test_check_too_old_raises() -> None:
    with pytest.raises(SchemaTooOld):
        check({"_schema_version": DAEMON_SCHEMA_VERSION - COMPATIBILITY_WINDOW - 1})


def test_save_and_load_artifact_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "a.json"
    save_artifact(p, {"x": 1})
    body = load_artifact(p)
    assert body["x"] == 1
    assert body["_schema_version"] == DAEMON_SCHEMA_VERSION


def test_migrate_runs_registered_chain() -> None:
    # Register a temporary migration v0→v1 that adds a field.
    @register_migration(0, 1)
    def _m(payload: dict) -> dict:
        payload["added"] = True
        return payload

    out = migrate({"_schema_version": 0})
    assert out["added"] is True
    assert out["_schema_version"] >= 1


# ---- Layer 23 ------------------------------------------------------------


def test_build_section_collects_god_nodes(snapshot: InMemoryGraph) -> None:
    section = build_section(snapshot)
    assert isinstance(section.god_nodes, list)


def test_render_section_has_markers(snapshot: InMemoryGraph) -> None:
    body = render_section(build_section(snapshot))
    assert "graphify-plus:start" in body
    assert "graphify-plus:end" in body
    assert "Routing rule" in body


def test_update_file_creates_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "CLAUDE.md"
    body = "<!-- graphify-plus:start -->\nhi\n<!-- graphify-plus:end -->"
    res = update_file(p, body)
    assert p.exists()
    assert res["action"] == "appended"
    assert "hi" in p.read_text()


def test_update_file_replaces_existing_section(tmp_path: Path) -> None:
    p = tmp_path / "CLAUDE.md"
    p.write_text("# x\n<!-- graphify-plus:start -->\nold\n<!-- graphify-plus:end -->\nfooter\n")
    new_body = "<!-- graphify-plus:start -->\nnew\n<!-- graphify-plus:end -->"
    res = update_file(p, new_body)
    assert res["action"] == "updated"
    text = p.read_text()
    assert "old" not in text
    assert "new" in text
    assert "footer" in text  # outside markers preserved


def test_strip_removes_owned_section() -> None:
    text = "alpha\n<!-- graphify-plus:start -->\nx\n<!-- graphify-plus:end -->\nbeta"
    out = strip_owned_section(text)
    assert "graphify-plus:start" not in out
    assert "alpha" in out
    assert "beta" in out


def test_update_all_known_creates_claude_md_only(tmp_path: Path) -> None:
    body = "<!-- graphify-plus:start -->\nx\n<!-- graphify-plus:end -->"
    results = update_all_known(tmp_path, body)
    assert (tmp_path / "CLAUDE.md").exists()
    # AGENTS.md not pre-existing; not created.
    assert not (tmp_path / "AGENTS.md").exists()
    assert any(r["path"].endswith("CLAUDE.md") for r in results)


def test_cli_claude_md_update(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["claude-md", "update", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    claude = repo / "CLAUDE.md"
    assert claude.exists()
    body = claude.read_text()
    assert "graphify-plus:start" in body


def test_cli_claude_md_strip(tmp_path: Path) -> None:
    p = tmp_path / "CLAUDE.md"
    p.write_text("outer\n<!-- graphify-plus:start -->\nx\n<!-- graphify-plus:end -->\n")
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["claude-md", "strip", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    text = p.read_text()
    assert "graphify-plus:start" not in text
    assert "outer" in text
