"""Tests for Batch H: writeback proposals (4.2), correctness sampling (14.4),
notifications (19), backward migrations (21.4), inline annotations (17.2)."""

from __future__ import annotations

from datetime import datetime
from datetime import time as dtime
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.correctness_sampling import (
    VerificationRecord,
    disagreement_rate,
    should_sample,
)
from graphify_plus.daemon.correctness_sampling import (
    record as record_verification,
)
from graphify_plus.daemon.lsp_shim import LSPServer
from graphify_plus.daemon.notifications import (
    NotificationConfig,
    Sink,
    dispatch,
    in_quiet_hours,
    is_rate_limited,
    load_config,
    mark_sent,
)
from graphify_plus.daemon.schema_version import (
    DAEMON_SCHEMA_VERSION,
    SchemaTooNew,
    declare_lossy,
    lost_in_migration,
    migrate,
    register_migration,
)
from graphify_plus.daemon.writeback_proposals import (
    decide,
    disable_auto_accept,
    enable_auto_accept,
    list_proposals,
    load_session,
    propose,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd

# ---- Layer 4.2 — writeback proposals -----------------------------------


def test_propose_creates_pending(repo: Path) -> None:
    p = propose(repo, target="sid-1", rationale="x")
    assert p.state == "pending"


def test_propose_idempotent(repo: Path) -> None:
    a = propose(repo, target="sid-1", rationale="x")
    b = propose(repo, target="sid-1", rationale="x")
    assert a.id == b.id
    rows = list_proposals(repo)
    assert len(rows) == 1


def test_decide_accepts(repo: Path) -> None:
    p = propose(repo, target="sid-1", rationale="x")
    out = decide(repo, p.id, accept=True)
    assert out is not None
    assert out.state == "accepted"


def test_decide_rejects(repo: Path) -> None:
    p = propose(repo, target="sid-1", rationale="x")
    out = decide(repo, p.id, accept=False)
    assert out is not None
    assert out.state == "rejected"


def test_auto_accept_records_immediately(repo: Path) -> None:
    enable_auto_accept(repo)
    p = propose(repo, target="sid-1", rationale="x")
    assert p.state == "accepted"
    disable_auto_accept(repo)
    p2 = propose(repo, target="sid-2", rationale="y")
    assert p2.state == "pending"


def test_load_session_roundtrip(repo: Path) -> None:
    s = enable_auto_accept(repo)
    assert s.auto_accept
    loaded = load_session(repo)
    assert loaded.auto_accept


def test_cli_writeback_full_flow(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd,
        ["writeback", "propose", "sid-x", "rationale text", "--repo", str(repo)],
    )
    assert result.exit_code == 0
    listing = runner.invoke(daemon_cmd, ["writeback", "list", "--repo", str(repo)])
    assert "sid-x" in listing.output


# ---- Layer 14.4 — correctness sampling --------------------------------


def test_should_sample_deterministic() -> None:
    """Same entity_id on the same day → same answer."""
    a = should_sample("sid-1", rate=0.5)
    b = should_sample("sid-1", rate=0.5)
    assert a == b


def test_should_sample_rate_zero_never() -> None:
    assert not should_sample("any", rate=0.0)


def test_should_sample_rate_one_always() -> None:
    assert should_sample("any", rate=1.0)


def test_record_verification(repo: Path) -> None:
    rec = VerificationRecord(
        entity_id="sid-1",
        primary_extractor="ast",
        primary_value="x",
        secondary_extractor="llm",
        secondary_value="x",
        disagreement=False,
    )
    record_verification(repo, rec)
    out = disagreement_rate(repo)
    assert out["samples"] == 1
    assert out["rate"] == 0.0


def test_disagreement_rate_aggregates(repo: Path) -> None:
    record_verification(
        repo,
        VerificationRecord(
            entity_id="a",
            primary_extractor="ast",
            primary_value=1,
            secondary_extractor="llm",
            secondary_value=1,
            disagreement=False,
        ),
    )
    record_verification(
        repo,
        VerificationRecord(
            entity_id="b",
            primary_extractor="ast",
            primary_value=1,
            secondary_extractor="llm",
            secondary_value=2,
            disagreement=True,
        ),
    )
    out = disagreement_rate(repo)
    assert out["samples"] == 2
    assert out["rate"] == 0.5


def test_cli_correctness_stats(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["correctness-stats", "--repo", str(repo), "--json"])
    assert result.exit_code == 0


# ---- Layer 19.x — notifications ---------------------------------------


def test_in_quiet_hours_simple() -> None:
    cfg = NotificationConfig(quiet_hours=[(dtime(8, 0), dtime(17, 0))])
    base = datetime(2026, 1, 1)
    inside = base.replace(hour=10, minute=0)
    outside = base.replace(hour=20, minute=0)
    assert in_quiet_hours(cfg, now=inside)
    assert not in_quiet_hours(cfg, now=outside)


def test_in_quiet_hours_crossing_midnight() -> None:
    cfg = NotificationConfig(quiet_hours=[(dtime(22, 0), dtime(8, 0))])
    base = datetime(2026, 1, 1)
    night = base.replace(hour=23, minute=0)
    morning = base.replace(hour=7, minute=0)
    midday = base.replace(hour=12, minute=0)
    assert in_quiet_hours(cfg, now=night)
    assert in_quiet_hours(cfg, now=morning)
    assert not in_quiet_hours(cfg, now=midday)


def test_load_config_missing(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg.sinks == []


def test_load_config_yaml(tmp_path: Path) -> None:
    p = tmp_path / ".graphify_plus" / "notifications.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(
        "rate_limit_minutes: 10\n"
        "quiet_hours:\n  - 22:00-08:00\n"
        "sinks:\n  - kind: webhook\n    url: https://example.invalid\n    events: [anomaly]\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.rate_limit_minutes == 10
    assert cfg.sinks
    assert cfg.sinks[0].kind == "webhook"


def test_dispatch_terminal_default(tmp_path: Path) -> None:
    """No sinks configured → terminal echo records appear."""
    out = dispatch(tmp_path, event="anomaly", title="t", body="b")
    assert any(r.get("sink") == "terminal" for r in out)


def test_rate_limit_blocks_repeats(tmp_path: Path) -> None:
    cfg = NotificationConfig(rate_limit_minutes=60)
    sink = Sink(kind="webhook", url="https://example.invalid")
    mark_sent(tmp_path, sink)
    assert is_rate_limited(tmp_path, sink, cfg)


def test_dispatch_quiet_hours_skips(tmp_path: Path) -> None:
    p = tmp_path / ".graphify_plus" / "notifications.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("quiet_hours:\n  - 00:00-23:59\n")
    out = dispatch(tmp_path, event="anomaly", title="t", body="b")
    assert any("_quiet_hours" in r.get("sink", "") for r in out)


# ---- Layer 21.4 — backward migrations ---------------------------------


def test_backward_migration_runs() -> None:
    @register_migration(2, 1)
    def _back(payload: dict) -> dict:
        return declare_lossy(
            {k: v for k, v in payload.items() if k != "extra_v2"}, dropped=["extra_v2"]
        )

    body = {"_schema_version": 2, "extra_v2": "x", "core": "y"}
    out = migrate(body, target=1)
    assert out["_schema_version"] == 1
    assert "extra_v2" not in out
    assert lost_in_migration(out) == ["extra_v2"]


def test_too_new_without_backward_migration() -> None:
    """Without a registered backward, going from current version
    +1 to current raises SchemaTooNew."""
    body = {"_schema_version": DAEMON_SCHEMA_VERSION + 5}
    import pytest

    with pytest.raises(SchemaTooNew):
        migrate(body, target=DAEMON_SCHEMA_VERSION)


# ---- Layer 17.2 — inline annotations ----------------------------------


def test_lsp_inlay_hints_dispatch(repo: Path) -> None:
    server = LSPServer(repo)
    out = server._dispatch(
        "textDocument/inlayHint",
        {"textDocument": {"uri": f"file://{repo}/auth.py"}},
    )
    assert isinstance(out, list)
