"""Tests for Layer 17 (LSP shim), 19 (anomaly/trend), 20 (audit/undo)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.anomaly import (
    HealthSnapshot,
    append_snapshot,
    ascii_sparkline,
    detect,
    history,
    render_trend,
    render_weekly,
    take_snapshot,
)
from graphify_plus.daemon.audit_log import (
    append as audit_append,
)
from graphify_plus.daemon.audit_log import (
    by_agent,
    current_namespace,
    hash_log,
    latest_state,
    read_all,
    revert,
)
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.lsp_shim import LSPServer, _read_message, _write_message
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd

# ---- Layer 19 ------------------------------------------------------------


def test_take_snapshot_includes_basic_fields(snapshot: InMemoryGraph) -> None:
    s = take_snapshot(snapshot, rule_violations=2)
    assert s.n_symbols == len(snapshot.by_id)
    assert s.rule_violations == 2
    assert s.fresh_token


def test_append_and_read_history(repo: Path, snapshot: InMemoryGraph) -> None:
    append_snapshot(repo, take_snapshot(snapshot))
    append_snapshot(repo, take_snapshot(snapshot))
    rows = history(repo)
    assert len(rows) == 2


def test_detect_god_node_drop() -> None:
    prev = HealthSnapshot(
        ts="t1",
        n_symbols=10,
        n_files=2,
        n_edges=10,
        god_node_count=10,
        fresh_token="x",
    )
    cur = HealthSnapshot(
        ts="t2",
        n_symbols=10,
        n_files=2,
        n_edges=10,
        god_node_count=4,
        fresh_token="y",
    )
    out = detect(prev, cur)
    assert any(a.code == "GOD_NODE_DROP" for a in out)


def test_detect_coverage_drop() -> None:
    prev = HealthSnapshot(
        ts="t1",
        n_symbols=10,
        n_files=2,
        n_edges=10,
        god_node_count=5,
        fresh_token="x",
        coverage_pct=80.0,
    )
    cur = HealthSnapshot(
        ts="t2",
        n_symbols=10,
        n_files=2,
        n_edges=10,
        god_node_count=5,
        fresh_token="y",
        coverage_pct=60.0,
    )
    out = detect(prev, cur)
    assert any(a.code == "COVERAGE_DROP" for a in out)


def test_detect_new_violations() -> None:
    prev = HealthSnapshot(
        ts="t1",
        n_symbols=10,
        n_files=2,
        n_edges=10,
        god_node_count=5,
        fresh_token="x",
        rule_violations=0,
    )
    cur = HealthSnapshot(
        ts="t2",
        n_symbols=10,
        n_files=2,
        n_edges=10,
        god_node_count=5,
        fresh_token="y",
        rule_violations=3,
    )
    out = detect(prev, cur)
    assert any(a.code == "NEW_RULE_VIOLATIONS" for a in out)


def test_ascii_sparkline_handles_constant_values() -> None:
    s = ascii_sparkline([1.0, 1.0, 1.0])
    assert len(s) == 3


def test_ascii_sparkline_downsamples_long_input() -> None:
    s = ascii_sparkline(list(range(200)), width=20)
    assert len(s) == 20


def test_render_trend_empty() -> None:
    assert "no health snapshots" in render_trend([])


def test_render_trend_with_rows(repo: Path, snapshot: InMemoryGraph) -> None:
    append_snapshot(repo, take_snapshot(snapshot))
    out = render_trend(history(repo))
    assert "health trend" in out


def test_render_weekly(repo: Path, snapshot: InMemoryGraph) -> None:
    append_snapshot(repo, take_snapshot(snapshot))
    out = render_weekly(repo, history(repo))
    assert "weekly digest" in out


def test_cli_trend_runs(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["trend", "--repo", str(repo)])
    assert result.exit_code == 0


def test_cli_trend_snapshot_then_show(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["trend", "--repo", str(repo), "--snapshot"])
    assert result.exit_code == 0


# ---- Layer 20 ------------------------------------------------------------


def test_audit_append_and_read(repo: Path) -> None:
    rec = audit_append(repo, kind="annotation", target="sid-1", payload={"note": "x"})
    rows = read_all(repo)
    assert len(rows) == 1
    assert rows[0].id == rec.id


def test_audit_revert_creates_tombstone(repo: Path) -> None:
    rec = audit_append(repo, kind="annotation", target="sid-1", payload={"note": "x"})
    revert(repo, rec.id)
    rows = read_all(repo)
    assert any(r.kind == "tombstone" for r in rows)
    state = latest_state(repo)
    # The tombstone removes the original from latest state.
    keys = list(state.keys())
    assert all(state[k].id != rec.id for k in keys)


def test_audit_namespace_default_is_branch(repo: Path) -> None:
    ns = current_namespace(repo)
    assert isinstance(ns, str)


def test_audit_by_agent(repo: Path) -> None:
    audit_append(repo, kind="annotation", agent="alice")
    audit_append(repo, kind="annotation", agent="alice")
    audit_append(repo, kind="annotation", agent="bob")
    counts = by_agent(repo)
    assert counts.get("alice") == 2
    assert counts.get("bob") == 1


def test_audit_hash_changes_on_append(repo: Path) -> None:
    audit_append(repo, kind="ingest", target="x")
    h1 = hash_log(repo)
    audit_append(repo, kind="ingest", target="y")
    h2 = hash_log(repo)
    assert h1 != h2


def test_cli_audit_log(repo: Path) -> None:
    audit_append(repo, kind="annotation", target="sid-1")
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["audit", "log", "--repo", str(repo)])
    assert result.exit_code == 0
    assert "annotation" in result.output


def test_cli_audit_revert(repo: Path) -> None:
    rec = audit_append(repo, kind="annotation", target="sid-1")
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["audit", "revert", rec.id, "--repo", str(repo)])
    assert result.exit_code == 0
    assert "tombstone" in result.output


# ---- Layer 17 ------------------------------------------------------------


def _send_request(server: LSPServer, in_buf: io.BytesIO, out_buf: io.BytesIO, msg: dict) -> dict:
    body = json.dumps(msg).encode("utf-8")
    in_buf.write(f"Content-Length: {len(body)}\r\n\r\n".encode())
    in_buf.write(body)


def test_lsp_initialize(repo: Path) -> None:
    """Call dispatch directly — exercising the JSON-RPC framing also
    works but is overkill for the unit-test goal."""
    server = LSPServer(repo)
    out = server._dispatch("initialize", {})
    assert out["capabilities"]["hoverProvider"] is True


def test_lsp_did_open_remembers_text(repo: Path) -> None:
    server = LSPServer(repo)
    server._dispatch(
        "textDocument/didOpen",
        {"textDocument": {"uri": "file:///x.py", "text": "hello"}},
    )
    assert "hello" in server._open_files["file:///x.py"]


def test_lsp_status_bar_no_daemon(repo: Path) -> None:
    server = LSPServer(repo)
    body = server._dispatch("graphifyPlus/statusBar", {})
    assert body["running"] is False


def test_lsp_uri_to_rel(repo: Path) -> None:
    server = LSPServer(repo)
    rel = server._uri_to_rel({"textDocument": {"uri": f"file://{repo}/auth.py"}})
    assert rel == "auth.py"


def test_lsp_message_round_trip() -> None:
    out = io.BytesIO()
    _write_message(out, {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    body = out.getvalue()
    in_buf = io.BytesIO(body)
    msg = _read_message(in_buf)
    assert msg is not None
    assert msg["result"]["ok"] is True
