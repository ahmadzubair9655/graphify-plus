"""Tests for Batch J: 6.2 (Slack stub), 7.4 (live profiler), 15.5 (hyperscale)."""

from __future__ import annotations

import json
from pathlib import Path

from graphify_plus.daemon.hyperscale import (
    Shard,
    ShardConfig,
    fan_out,
    load_shards,
)
from graphify_plus.daemon.live_profiler import (
    LiveAttachConfig,
    detect_adapters,
)
from graphify_plus.daemon.slack_ingest import (
    ChatAllowlist,
    ChatMessage,
    filter_by_allowlist,
    ingest_slack,
    load_allowlist,
    parse_slack_export,
    threads_from_messages,
)
from graphify_plus.runtime.store import Store, cache_path

# ---- Layer 6.2 — Slack ingest ------------------------------------------


def test_load_allowlist_missing(tmp_path: Path) -> None:
    al = load_allowlist(tmp_path)
    assert al.channels == []


def test_load_allowlist_yaml(tmp_path: Path) -> None:
    p = tmp_path / ".graphify_plus" / "chat-allowlist.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("channels:\n  - eng-decisions\n  - eng-architecture\nusers_excluded:\n  - bot1\n")
    al = load_allowlist(tmp_path)
    assert "eng-decisions" in al.channels
    assert "bot1" in al.users_excluded


def test_filter_by_allowlist_drops_uncovered() -> None:
    al = ChatAllowlist(channels=["eng"])
    msgs = [
        ChatMessage(ts="1", user="alice", text="hi", channel="random"),
        ChatMessage(ts="2", user="bob", text="hello", channel="eng"),
    ]
    out = filter_by_allowlist(msgs, al)
    assert len(out) == 1
    assert out[0].channel == "eng"


def test_filter_by_allowlist_drops_users() -> None:
    al = ChatAllowlist(channels=["eng"], users_excluded=["bot1"])
    msgs = [
        ChatMessage(ts="1", user="bot1", text="x", channel="eng"),
        ChatMessage(ts="2", user="alice", text="y", channel="eng"),
    ]
    out = filter_by_allowlist(msgs, al)
    assert len(out) == 1
    assert out[0].user == "alice"


def test_threads_from_messages_groups() -> None:
    msgs = [
        ChatMessage(ts="1.0", user="a", text="parent", channel="eng", thread_id="1.0"),
        ChatMessage(ts="2.0", user="b", text="reply", channel="eng", thread_id="1.0"),
    ]
    nodes = threads_from_messages(msgs)
    assert len(nodes) == 1
    assert "parent" in nodes[0].body
    assert "reply" in nodes[0].body


def test_parse_slack_export(tmp_path: Path) -> None:
    channel_dir = tmp_path / "general"
    channel_dir.mkdir()
    (channel_dir / "2024-01-01.json").write_text(
        json.dumps(
            [
                {"ts": "1.0", "user": "alice", "text": "hello"},
                {"ts": "2.0", "user": "bob", "text": "world", "thread_ts": "1.0"},
            ]
        )
    )
    msgs = parse_slack_export(tmp_path)
    assert len(msgs) == 2
    assert any(m.text == "hello" for m in msgs)


def test_ingest_slack_skips_without_allowlist(repo: Path, tmp_path: Path) -> None:
    archive = tmp_path / "exp"
    archive.mkdir()
    store = Store(cache_path(repo))
    try:
        out = ingest_slack(store, repo, archive)
    finally:
        store.close()
    assert out["skipped"] is True


def test_ingest_slack_with_allowlist(repo: Path, tmp_path: Path) -> None:
    al = repo / ".graphify_plus" / "chat-allowlist.yaml"
    al.parent.mkdir(parents=True, exist_ok=True)
    al.write_text("channels:\n  - eng\n")
    archive = tmp_path / "exp"
    eng = archive / "eng"
    eng.mkdir(parents=True)
    (eng / "1.json").write_text(
        json.dumps([{"ts": "1.0", "user": "alice", "text": "decided to use redis"}])
    )
    store = Store(cache_path(repo))
    try:
        out = ingest_slack(store, repo, archive)
    finally:
        store.close()
    assert out["threads"] >= 1


# ---- Layer 7.4 — live profiler ----------------------------------------


def test_detect_adapters_returns_list() -> None:
    adapters = detect_adapters()
    names = {a.name for a in adapters}
    assert {"py-spy", "node-inspect", "generic-ps"} == names


def test_attach_py_spy_no_binary(monkeypatch) -> None:
    """Returns None when py-spy isn't on PATH."""
    monkeypatch.setattr("graphify_plus.daemon.live_profiler.shutil.which", lambda _: None)
    from graphify_plus.daemon.live_profiler import attach_py_spy

    assert attach_py_spy(1234) is None


def test_live_attach_once_unimplemented_adapter() -> None:
    from graphify_plus.daemon.live_profiler import live_attach_once

    out = live_attach_once(LiveAttachConfig(pid=1234, adapter="not-real"))
    assert out is None


# ---- Layer 15.5 — hyperscale -----------------------------------------


def test_load_shards_missing(tmp_path: Path) -> None:
    cfg = load_shards(tmp_path)
    assert cfg.shards == []


def test_load_shards_yaml(tmp_path: Path) -> None:
    p = tmp_path / ".graphify_plus" / "shards.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("shards:\n  - name: api\n    path: /tmp/api\n  - name: web\n    path: /tmp/web\n")
    cfg = load_shards(tmp_path)
    assert {s.name for s in cfg.shards} == {"api", "web"}


def test_fan_out_no_shards() -> None:
    cfg = ShardConfig(shards=[])
    out = fan_out(cfg, "graph_stats", {})
    assert out["results"] == []
    assert out["shards"] == []


def test_fan_out_aggregates(monkeypatch) -> None:
    """When the daemons are mocked to return rows, fan_out merges them
    and tags each row with its shard name."""

    class FakeClient:
        def __init__(self, repo, *_a, **_kw):
            self.repo = repo

        def call(self, op, args):
            return {
                "ok": True,
                "results": [{"label": f"x-from-{self.repo.name}"}],
                "more_available": 0,
            }

    monkeypatch.setattr("graphify_plus.daemon.hyperscale.DaemonClient", FakeClient)
    cfg = ShardConfig(shards=[Shard(name="api", repo_path=Path("/tmp/api"))])
    out = fan_out(cfg, "find_by_name", {"label": "x"})
    assert out["results"]
    assert out["results"][0]["shard"] == "api"
    assert out["shards"][0]["count"] == 1
