"""Phase 6 CLIs: ``gp simulate``, ``gp guardrails``, ``gp drift``.

All three sit on the unified rules engine in ``runtime/rules.py``.
Generated reports are always written into the target repo, never into
the graphify-plus tree itself.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
from pathlib import Path

import click

from ...core.symbol_graph import build as build_graph
from ...query.drift import DriftDaemon, render_report
from ...query.drift import check as drift_check
from ...query.guardrails import evaluate_payload
from ...query.shadow import simulate as shadow_simulate
from ...runtime.overlay import empty as empty_overlay
from ...runtime.rules import RuleSet
from ...runtime.store import Store, cache_path

RULES_FILENAME = "rules.yaml"


def _load_ruleset(repo: Path) -> RuleSet:
    rules_path = repo / ".graphify_plus" / RULES_FILENAME
    if not rules_path.exists():
        return RuleSet()
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        # PyYAML isn't a base dep — accept JSON-as-YAML as fallback.
        return RuleSet.from_dict(json.loads(rules_path.read_text()))
    return RuleSet.from_dict(yaml.safe_load(rules_path.read_text()) or {})


def _resolve_node(store: Store, ref: str) -> str | None:
    if store.get_symbol(ref) is not None:
        return ref
    matches = store.find_symbols_by_qualified_name(ref)
    return matches[0]["id"] if matches else None


def _open_store(repo: Path) -> Store:
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    return Store(cache_path(repo))


# ---- gp simulate -----------------------------------------------------------


@click.command("simulate")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--add-edge", multiple=True, help="src->dst[:kind] (default kind=calls).")
@click.option("--remove-edge", multiple=True, help="src->dst[:kind].")
@click.option("--add-node", multiple=True, help="node_id (kind=stub, path='').")
@click.option("--rm-node", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def simulate_cmd(
    repo: Path,
    add_edge: tuple[str, ...],
    remove_edge: tuple[str, ...],
    add_node: tuple[str, ...],
    rm_node: tuple[str, ...],
    as_json: bool,
) -> None:
    """Stage a hypothetical change and grade it. Read-only."""
    repo = repo.resolve()
    store = _open_store(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        overlay = empty_overlay(G)
        for raw in add_node:
            overlay.add_node(raw, kind="stub", path="", name=raw)
        for raw in rm_node:
            sid = _resolve_node(store, raw) or raw
            overlay.remove_node(sid)
        for raw in add_edge:
            spec, _, kind = raw.partition(":")
            kind = kind or "calls"
            src, _, dst = spec.partition("->")
            sid = _resolve_node(store, src.strip()) or src.strip()
            did = _resolve_node(store, dst.strip()) or dst.strip()
            if not G.has_node(did):
                overlay.add_node(did, kind="stub", path="", name=did)
            overlay.add_edge(sid, did, kind=kind)
        for raw in remove_edge:
            spec, _, kind = raw.partition(":")
            src, _, dst = spec.partition("->")
            sid = _resolve_node(store, src.strip()) or src.strip()
            did = _resolve_node(store, dst.strip()) or dst.strip()
            overlay.remove_edge(sid, did, kind=kind or None)

        result = shadow_simulate(overlay, _load_ruleset(repo))
    finally:
        store.close()

    if as_json:
        click.echo(
            json.dumps(
                {
                    "grade": result.grade,
                    "summary": result.summary,
                    "violations": [
                        {
                            "rule_id": v.rule_id,
                            "severity": v.severity,
                            "src": v.src,
                            "dst": v.dst,
                            "kind": v.kind,
                            "message": v.message,
                        }
                        for v in result.violations
                    ],
                },
                indent=2,
            )
        )
        return

    click.echo(f"shadow: {result.summary}")
    for v in result.violations:
        click.echo(f"  [{v.severity}] {v.rule_id}: {v.message}")


# ---- gp guardrails --------------------------------------------------------


@click.command("guardrails")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option(
    "--proposed-edit-json",
    type=str,
    default=None,
    help="JSON payload (or read from stdin if omitted).",
)
def guardrails_cmd(repo: Path, proposed_edit_json: str | None) -> None:
    """Pre-tool-use check: would this proposed edit violate any rule?"""
    repo = repo.resolve()
    payload = (
        json.loads(proposed_edit_json)
        if proposed_edit_json
        else json.loads(sys.stdin.read() or "{}")
    )
    store = _open_store(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        decision = evaluate_payload(store, G, payload, _load_ruleset(repo))
    finally:
        store.close()

    if not decision.block:
        click.echo("OK")
        return
    click.echo(
        json.dumps(
            {
                "block": True,
                "rule_id": decision.rule_id,
                "suggestion": decision.suggestion,
                "violations": [
                    {
                        "rule_id": v.rule_id,
                        "severity": v.severity,
                        "src": v.src,
                        "dst": v.dst,
                        "kind": v.kind,
                        "message": v.message,
                    }
                    for v in decision.violations
                ],
            },
            indent=2,
        )
    )
    sys.exit(1)


# ---- gp drift -------------------------------------------------------------


@click.group("drift")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.pass_context
def drift_cmd(ctx: click.Context, repo: Path) -> None:
    """One-shot drift check or live drift watcher."""
    ctx.ensure_object(dict)
    ctx.obj["repo"] = repo.resolve()


@drift_cmd.command("check")
@click.option(
    "--write/--no-write",
    default=True,
    help="Write .graphify_plus/drift_report.md alongside printing.",
)
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def drift_check_cmd(ctx: click.Context, write: bool, as_json: bool) -> None:
    repo: Path = ctx.obj["repo"]
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    report = drift_check(repo, _load_ruleset(repo))

    if write:
        out = repo / ".graphify_plus" / "drift_report.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_report(report))

    if as_json:
        click.echo(
            json.dumps(
                {
                    "grade": report.grade,
                    "violations": [
                        {
                            "rule_id": v.rule_id,
                            "severity": v.severity,
                            "src": v.src,
                            "dst": v.dst,
                            "kind": v.kind,
                            "message": v.message,
                        }
                        for v in report.violations
                    ],
                },
                indent=2,
            )
        )
        return
    click.echo(f"drift: grade={report.grade} violations={len(report.violations)}")
    for v in report.violations:
        click.echo(f"  [{v.severity}] {v.rule_id}: {v.message}")


@drift_cmd.command("watch")
@click.pass_context
def drift_watch_cmd(ctx: click.Context) -> None:
    repo: Path = ctx.obj["repo"]
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )

    stopped = threading.Event()
    daemon = DriftDaemon(repo, _load_ruleset(repo))

    def _on_event(report) -> None:
        click.echo(
            f"drift: grade={report.grade} violations={len(report.violations)}",
            err=True,
        )

    daemon.start(_on_event)

    def _shutdown(signum, frame):  # noqa: ARG001
        stopped.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    click.echo(f"gp drift watch: {repo} (Ctrl-C to stop)", err=True)
    try:
        stopped.wait()
    finally:
        daemon.stop()


__all__ = ["drift_cmd", "guardrails_cmd", "simulate_cmd"]
