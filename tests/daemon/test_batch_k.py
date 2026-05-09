"""Tests for Batch K: more aliases, Node profiler adapter, rebuild-from-log,
auto-changelog, MCP coverage."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from graphify_plus.daemon.audit_log import (
    append as audit_append,
    rebuild_from_log,
    revert,
)
from graphify_plus.daemon.changelog import (
    CONVENTIONAL_RE,
    CommitEntry,
    collect_commits,
    group,
    make_changelog,
    render,
)
from graphify_plus.daemon.handlers import HANDLERS
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.live_profiler import (
    LiveAttachConfig,
    attach_node_inspect,
    live_attach_once,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd
from graphify_plus.interface.mcp_server import TOOLS


# ---- Layer 5.4 — more aliases ------------------------------------------


def test_alias_propose_fixes_present() -> None:
    assert "propose_fixes" in HANDLERS


def test_alias_who_uses_present() -> None:
    assert "who_uses" in HANDLERS


def test_alias_search_present() -> None:
    assert "search" in HANDLERS


def test_aliases_are_callable(snapshot: InMemoryGraph) -> None:
    for op in ("who_uses", "uses_what", "search", "propose_fixes"):
        out = HANDLERS[op](snapshot, {"node": "AuthService", "query": "auth"})
        assert "results" in out


# ---- Layer 7.4 — Node profiler adapter --------------------------------


def test_attach_node_inspect_invalid_pid(tmp_path: Path) -> None:
    """SIGUSR1 to a definitely-invalid PID returns None."""
    out = attach_node_inspect(2_147_483_646, output=tmp_path / "out.txt")
    assert out is None


def test_live_attach_once_routes_to_node(monkeypatch, tmp_path: Path) -> None:
    """Confirm the dispatcher reaches attach_node_inspect when adapter='node-inspect'."""
    called = {"hit": False}

    def fake_attach(pid: int, **kw: object) -> Path:
        called["hit"] = True
        return tmp_path / "fake.txt"

    monkeypatch.setattr(
        "graphify_plus.daemon.live_profiler.attach_node_inspect", fake_attach
    )
    out = live_attach_once(LiveAttachConfig(pid=1234, adapter="node-inspect"))
    assert called["hit"]


# ---- Layer 20.4 — rebuild from log ------------------------------------


def test_rebuild_from_log_groups_by_kind(repo: Path) -> None:
    audit_append(repo, kind="annotation", target="sid-1", payload={"note": "x"})
    audit_append(repo, kind="correction", target="sid-1")
    audit_append(repo, kind="ingest", target="github")
    out = rebuild_from_log(repo)
    assert len(out["annotations_to_apply"]) == 1
    assert len(out["corrections_to_apply"]) == 1
    assert len(out["ingests_seen"]) == 1
    assert "log_hash" in out


def test_rebuild_from_log_excludes_tombstoned(repo: Path) -> None:
    a = audit_append(repo, kind="annotation", target="sid-1")
    revert(repo, a.id)
    out = rebuild_from_log(repo)
    # Tombstoned annotation is dropped from the apply list.
    assert all(r["id"] != a.id for r in out["annotations_to_apply"])


def test_cli_audit_rebuild(repo: Path) -> None:
    audit_append(repo, kind="annotation", target="sid-1")
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["audit-rebuild", "--repo", str(repo), "--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert "annotations_to_apply" in body


# ---- Layer 27.4 — auto-changelog --------------------------------------


def test_conventional_re_matches() -> None:
    m = CONVENTIONAL_RE.match("feat(daemon): add X")
    assert m is not None
    assert m.group("type") == "feat"
    assert m.group("scope") == "(daemon)"
    assert m.group("msg") == "add X"


def test_conventional_re_skips_non_conv() -> None:
    m = CONVENTIONAL_RE.match("just a message")
    assert m is None


def test_collect_commits_handles_no_git(tmp_path: Path) -> None:
    """Outside a git repo: returns empty rather than crashing."""
    out = collect_commits(tmp_path, "HEAD~5", "HEAD")
    assert isinstance(out, list)


def test_group_separates_conventional_from_other() -> None:
    rows = [
        CommitEntry(sha="a" * 40, short_sha="aaaa", type="feat", scope="x", message="m1", full_subject="feat(x): m1"),
        CommitEntry(sha="b" * 40, short_sha="bbbb", type="other", scope="", message="random", full_subject="random"),
    ]
    rep = group(rows)
    assert "feat" in rep.by_type
    assert len(rep.other) == 1


def test_render_changelog_full() -> None:
    rep = group(
        [
            CommitEntry(sha="1", short_sha="1", type="feat", scope="daemon", message="add X", full_subject="feat(daemon): add X"),
            CommitEntry(sha="2", short_sha="2", type="fix", scope="", message="fix Y", full_subject="fix: fix Y"),
            CommitEntry(sha="3", short_sha="3", type="other", scope="", message="random", full_subject="random"),
        ]
    )
    rep.since_ref = "v1.0.0"
    rep.head_ref = "HEAD"
    out = render(rep)
    assert "Added" in out
    assert "Fixed" in out
    assert "Other" in out
    assert "add X" in out


def test_make_changelog_runs(tmp_path: Path) -> None:
    rep = make_changelog(tmp_path, "no-such-ref")
    assert rep.since_ref == "no-such-ref"


def test_cli_changelog(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["changelog", "--since", "no-such-ref", "--repo", str(tmp_path)]
    )
    assert result.exit_code == 0


# ---- MCP coverage -----------------------------------------------------


def test_mcp_has_pre_edit_tool() -> None:
    assert "gp_pre_edit" in TOOLS


def test_mcp_has_diagnose_tool() -> None:
    assert "gp_diagnose" in TOOLS


def test_mcp_has_cross_stack_tool() -> None:
    assert "gp_cross_stack" in TOOLS


def test_mcp_has_session_status_tool() -> None:
    assert "gp_session_status" in TOOLS


def test_mcp_pre_edit_invocable_in_process(repo: Path) -> None:
    from graphify_plus.interface.mcp_server import tool_pre_edit

    out = tool_pre_edit({"repo": str(repo), "target": "AuthService.login"})
    assert out["ok"]
    assert "pre_edit" in out["extra"]


def test_mcp_diagnose_invocable(repo: Path) -> None:
    from graphify_plus.interface.mcp_server import tool_diagnose

    out = tool_diagnose({"repo": str(repo)})
    assert out["ok"]
    assert "daemon" in out["extra"]


def test_mcp_session_status_invocable(repo: Path) -> None:
    from graphify_plus.interface.mcp_server import tool_session_status

    out = tool_session_status({"repo": str(repo)})
    assert out["ok"]
    assert "daemon_running" in out["extra"]
