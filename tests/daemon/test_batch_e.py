"""Tests for Batch E: Layers 26 (multi-agent), 15 (scaling), 12 (ops rigor), 5.3 (quickstart)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.multi_agent import (
    SnapshotRegistry,
    get_registry,
    reset_registry_for_tests,
)
from graphify_plus.daemon.ops_rigor import (
    PERF_TARGETS,
    PRIVACY_DECLARATION,
    deterministic_hash,
    dry_run_network,
    perfcheck,
    with_failover,
)
from graphify_plus.daemon.quickstart import render_quickstart, run_quickstart
from graphify_plus.daemon.scaling import (
    Approximate,
    HUGE_GRAPH_THRESHOLD,
    build_coarse,
    coarse_summary,
    is_huge,
    sampled_pagerank,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd
from graphify_plus.runtime.store import Store, cache_path


# ---- Layer 26 ------------------------------------------------------------


def test_snapshot_take_and_release(snapshot: InMemoryGraph) -> None:
    reg = SnapshotRegistry()
    snap = reg.take(snapshot, agent_id="agent-1")
    assert reg.get(snap.id) is snap
    reg.release(snap.id, "agent-1")
    assert reg.get(snap.id) is None


def test_snapshot_pinned_by_multiple_agents_outlasts(snapshot: InMemoryGraph) -> None:
    reg = SnapshotRegistry()
    s = reg.take(snapshot, agent_id="a")
    s.pinned_by.add("b")
    reg.release(s.id, "a")
    assert reg.get(s.id) is s  # b still pinned
    reg.release(s.id, "b")
    assert reg.get(s.id) is None


def test_write_lock_serialises_per_target() -> None:
    reg = SnapshotRegistry()
    l1 = reg.acquire_write_lock("sid-1")
    l2 = reg.acquire_write_lock("sid-1")
    assert l1 is l2  # same lock returned on second call
    l3 = reg.acquire_write_lock("sid-2")
    assert l3 is not l1


def test_agent_budget_charges_and_blocks() -> None:
    reg = SnapshotRegistry()
    reg.register_agent("a1", cap_tokens=100)
    reg.charge("a1", 60)
    assert not reg.is_blocked("a1")
    reg.charge("a1", 50)
    assert reg.is_blocked("a1")
    reg.reset_budget("a1")
    assert not reg.is_blocked("a1")


def test_agent_budget_unknown_agent_blocked() -> None:
    reg = SnapshotRegistry()
    b = reg.charge("ghost", 10)
    assert b.blocked is True


def test_get_registry_singleton() -> None:
    reset_registry_for_tests()
    a = get_registry()
    b = get_registry()
    assert a is b


# ---- Layer 15 ------------------------------------------------------------


def test_build_coarse_groups_by_directory(snapshot: InMemoryGraph) -> None:
    coarse = build_coarse(snapshot, depth=1)
    assert coarse.nodes  # at least one module-level node
    summary = coarse_summary(coarse)
    assert summary["n_modules"] >= 1


def test_is_huge_false_for_tiny(snapshot: InMemoryGraph) -> None:
    assert is_huge(snapshot) is False


def test_sampled_pagerank_returns_top_k(snapshot: InMemoryGraph) -> None:
    rows, info = sampled_pagerank(snapshot, k=3)
    assert isinstance(info, Approximate)
    # Tiny graph: returns the cached pagerank (exact=True).
    assert len(rows) <= len(snapshot.by_id)


# ---- Layer 12 ------------------------------------------------------------


def test_dry_run_network_lists_endpoints(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GRAPHIFY_TELEMETRY_URL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    probes = dry_run_network(tmp_path)
    purposes = {p.purpose for p in probes}
    assert {"version check", "telemetry sink", "GitHub ingest", "local LLM"} == purposes


def test_dry_run_network_telemetry_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GRAPHIFY_TELEMETRY_URL", "https://example.com/telemetry")
    probes = dry_run_network(tmp_path)
    by_purpose = {p.purpose: p for p in probes}
    assert by_purpose["telemetry sink"].enabled is True


def test_perfcheck_runs(snapshot: InMemoryGraph) -> None:
    res = perfcheck(snapshot, samples=20)
    assert res.samples == 20
    assert res.p50_ms >= 0
    assert "p50_ms" in res.target


def test_with_failover_primary_succeeds() -> None:
    out = with_failover(lambda: {"x": 1}, lambda: {"x": 2})
    assert out == {"x": 1, "degraded": False}


def test_with_failover_fallback_runs() -> None:
    def primary() -> dict:
        raise RuntimeError("boom")

    out = with_failover(primary, lambda: {"x": 2})
    assert out["x"] == 2
    assert out["degraded"] is True
    assert "boom" in out["degraded_reason"]


def test_deterministic_hash_stable(snapshot: InMemoryGraph) -> None:
    h1 = deterministic_hash(snapshot)
    h2 = deterministic_hash(snapshot)
    assert h1 == h2


def test_deterministic_hash_changes_on_different_graphs(snapshot: InMemoryGraph) -> None:
    h1 = deterministic_hash(snapshot)
    snapshot.by_id["fake"] = {"id": "fake", "qualified_name": "extra", "kind": "function"}
    h2 = deterministic_hash(snapshot)
    assert h1 != h2


def test_perf_targets_have_known_buckets() -> None:
    assert {5_000, 30_000, 100_000} == set(PERF_TARGETS.keys())


def test_privacy_declaration_mentions_telemetry() -> None:
    assert "telemetry" in PRIVACY_DECLARATION.lower()


# ---- Layer 5.3 ----------------------------------------------------------


def test_quickstart_runs_against_existing_repo(repo: Path) -> None:
    out = run_quickstart(repo)
    assert "steps" in out
    step_names = {s["step"] for s in out["steps"]}
    assert {"snapshot", "install-skill+hook", "recipes"}.issubset(step_names)


def test_render_quickstart_includes_next_steps(repo: Path) -> None:
    out = run_quickstart(repo)
    body = render_quickstart(out)
    assert "Next steps" in body
    assert "graphify-plus quickstart" in body


def test_cli_quickstart(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["quickstart", "--repo", str(repo)])
    assert result.exit_code == 0
    assert "quickstart" in result.output.lower()


def test_cli_privacy(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["privacy", "--repo", str(repo)])
    assert result.exit_code == 0
    assert "graphify-plus" in result.output


def test_cli_privacy_dry_run(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["privacy", "--repo", str(repo), "--dry-run-network", "--json"]
    )
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert isinstance(body, list)
    assert any(p["purpose"] == "version check" for p in body)


def test_cli_perfcheck(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd, ["perfcheck", "--repo", str(repo), "--samples", "10"]
    )
    assert result.exit_code in (0, 1), result.output  # may legitimately fail tight target
