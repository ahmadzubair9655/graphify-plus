"""``gp daemon`` — start/stop/query the in-memory graph daemon.

Subcommands:

    gp daemon start     # foreground server
    gp daemon start --detach
    gp daemon stop
    gp daemon status
    gp daemon refresh
    gp daemon query OP [--arg KEY=VALUE ...]

This is the command surface for Sprint 1 of the master plan. It replaces
the cold "load + traverse + serialize" path used by every other CLI
verb. The intent-typed tools (whats_in, who_calls, what_depends_on,
find_by_name, find_by_concept, whats_central) all dispatch through
``query``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import click

from ...daemon import DaemonClient, DaemonNotRunning, pid_path, socket_path
from ...daemon.client import DaemonError
from ...daemon.receipts import format_receipt_line
from ...daemon.server import run_server


@click.group("daemon")
def daemon_cmd() -> None:
    """Manage the in-memory graph daemon."""


@daemon_cmd.command("start")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--detach", is_flag=True, help="Start in the background and return immediately.")
@click.option(
    "--no-watch",
    is_flag=True,
    help="Skip the embedded file watcher (use when you run `gp watch` separately).",
)
def start_cmd(repo: Path, detach: bool, no_watch: bool) -> None:
    repo = repo.resolve()
    client = DaemonClient(repo)
    if client.is_running():
        click.echo(f"daemon already running on {socket_path(repo)}", err=True)
        return
    watch = not no_watch
    if not detach:
        rc = run_server(repo, watch=watch)
        sys.exit(rc)
    pid = os.fork()
    if pid > 0:
        # Parent: wait briefly for the child to bind, then bail out.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if client.is_running():
                click.echo(
                    f"daemon started (pid={pid}) on {socket_path(repo)}",
                    err=True,
                )
                return
            time.sleep(0.05)
        click.echo("daemon did not become ready within 5s", err=True)
        sys.exit(1)
    # Child: detach into a new session and run.
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    sys.exit(run_server(repo, watch=watch))


@daemon_cmd.command("stop")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def stop_cmd(repo: Path) -> None:
    repo = repo.resolve()
    client = DaemonClient(repo)
    if not client.is_running():
        click.echo("no daemon running", err=True)
        return
    pid = _read_pid(repo)
    client.shutdown()
    # Verify the process is gone or send SIGTERM after a grace period.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if pid is None or not _pid_alive(pid):
            click.echo("daemon stopped")
            return
        time.sleep(0.05)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            click.echo(f"sent SIGTERM to pid={pid}")
        except ProcessLookupError:
            click.echo("daemon stopped")


@daemon_cmd.command("status")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def status_cmd(repo: Path, as_json: bool) -> None:
    repo = repo.resolve()
    client = DaemonClient(repo)
    if not client.is_running():
        if as_json:
            click.echo(json.dumps({"running": False}))
        else:
            click.echo("daemon: not running")
        sys.exit(1)
    info = client.call("graph_stats")
    extra = info.get("extra", {})
    fresh = info.get("freshness", {})
    if as_json:
        click.echo(json.dumps({"running": True, "stats": extra, "freshness": fresh}, indent=2))
        return
    click.echo(f"daemon: running ({socket_path(repo)})")
    click.echo(f"  symbols   : {extra.get('symbols', 0)}")
    click.echo(f"  files     : {extra.get('files', 0)}")
    click.echo(f"  edges_out : {extra.get('edges_out', 0)}")
    click.echo(f"  pagerank  : top {extra.get('pagerank_top_n', 0)}")
    click.echo(f"  build     : {extra.get('build_elapsed_ms', 0):.1f}ms")
    click.echo(f"  trust     : {fresh.get('trust', '?')}")
    if fresh.get("hint"):
        click.echo(f"  hint      : {fresh['hint']}")


@daemon_cmd.command("refresh")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def refresh_cmd(repo: Path) -> None:
    repo = repo.resolve()
    client = DaemonClient(repo)
    if not client.is_running():
        raise click.ClickException("daemon not running — `gp daemon start`")
    resp = client.call("refresh")
    extra = resp.get("extra", {})
    click.echo(
        f"refreshed: {extra.get('symbols', 0)} symbols, "
        f"{extra.get('files', 0)} files, "
        f"{extra.get('elapsed_ms', 0):.1f}ms, "
        f"token={extra.get('freshness_token', '')}"
    )


@daemon_cmd.group("coverage")
def coverage_group() -> None:
    """Test-coverage overlay (`gp daemon coverage ingest|summary|untested`)."""


@coverage_group.command("ingest")
@click.argument("report", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def coverage_ingest_cmd(report: Path, repo: Path) -> None:
    """Parse a coverage report and attribute hits to symbols.

    Supports Cobertura XML (``coverage xml`` / ``pytest --cov-report=xml``)
    and Istanbul JSON (``jest --coverage`` / ``vitest --coverage``).
    """
    from ...daemon.coverage import ingest_report
    from ...runtime.store import Store, cache_path as _cache_path

    repo = repo.resolve()
    if not _cache_path(repo).exists():
        raise click.ClickException(
            f"no graph cache at {_cache_path(repo)} — run `gp init --repo {repo}` first"
        )
    store = Store(_cache_path(repo))
    try:
        summary = ingest_report(store, repo, report)
    finally:
        store.close()
    click.echo(
        f"ingested {report.name} ({summary['format']}): "
        f"{summary['files']} files, "
        f"{summary['symbols_attributed']} symbols, "
        f"{summary['report_covered_lines']}/{summary['report_total_lines']} lines covered"
    )
    # Refresh the daemon if it's running so subsequent queries see new data.
    client = DaemonClient(repo)
    if client.is_running():
        client.call("refresh")
        click.echo("daemon refreshed")


@coverage_group.command("summary")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--top-k", default=10, type=int, help="Number of worst-covered files to show.")
@click.option("--json", "as_json", is_flag=True)
def coverage_summary_cmd(repo: Path, top_k: int, as_json: bool) -> None:
    """Repo-wide coverage stats."""
    repo = repo.resolve()
    args: dict[str, Any] = {"top_k": top_k}
    payload = _route_intent(repo, "coverage_summary", args)
    if "error" in payload:
        raise click.ClickException(payload["error"]["message"])
    extra = payload.get("extra", {})
    if as_json:
        click.echo(json.dumps(extra, indent=2))
        return
    if "reason" in extra:
        click.echo(extra["reason"])
        return
    click.echo(
        f"overall: {extra['overall_pct']}% "
        f"({extra['lines_covered']}/{extra['lines_total']} lines, "
        f"{extra['files_with_signal']} files)"
    )
    click.echo("")
    click.echo("worst-covered files:")
    for w in extra.get("worst_files", []):
        click.echo(f"  {w['pct']:>5.1f}%  {w['path']}  ({w['covered']}/{w['total']})")


@coverage_group.command("untested")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--path", default="", help="File or directory to scope to.")
@click.option("--max-pct", default=10.0, type=float, help="Max coverage % to include.")
@click.option("--json", "as_json", is_flag=True)
def coverage_untested_cmd(repo: Path, path: str, max_pct: float, as_json: bool) -> None:
    """List symbols at or below MAX_PCT coverage."""
    repo = repo.resolve()
    payload = _route_intent(
        repo, "whats_untested", {"path": path, "max_pct": max_pct, "budget_tokens": 4000}
    )
    if "error" in payload:
        raise click.ClickException(payload["error"]["message"])
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    rows = payload.get("results", [])
    if not rows:
        extra = payload.get("extra", {})
        click.echo(extra.get("reason", "no untested symbols below threshold"))
        return
    for row in rows:
        click.echo(
            f"{row.get('coverage_pct', 0.0):>5.1f}%  "
            f"{row.get('label', '?')}  [{row.get('kind', '?')}]  "
            f"{row.get('source_file', '?')}:{row.get('line_number', 0)}  "
            f"({row.get('coverage_lines', '0/0')})"
        )


@daemon_cmd.group("rules")
def rules_group() -> None:
    """Architectural-drift rules (`gp daemon rules check`)."""


@rules_group.command("check")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--fail-on-error",
    is_flag=True,
    help="Exit non-zero when any error-severity violation is found.",
)
def rules_check_cmd(repo: Path, as_json: bool, fail_on_error: bool) -> None:
    """Evaluate ``.graphify_plus/rules.yaml`` against the live graph."""
    repo = repo.resolve()
    payload = _route_intent(repo, "rules_check", {"budget_tokens": 4000})
    if "error" in payload:
        raise click.ClickException(payload["error"]["message"])
    extra = payload.get("extra", {})
    rows = payload.get("results", [])
    if as_json:
        click.echo(json.dumps({"extra": extra, "violations": rows}, indent=2))
        if fail_on_error and extra.get("errors", 0) > 0:
            sys.exit(1)
        return
    if extra.get("rule_count", 0) == 0:
        click.echo(extra.get("reason", "no ruleset configured"))
        return
    grade = extra.get("grade", "?")
    n_err = extra.get("errors", 0)
    n_warn = extra.get("warnings", 0)
    click.echo(
        f"grade {grade}  "
        f"errors={n_err}  warnings={n_warn}  "
        f"rules={extra.get('rule_count', 0)}"
    )
    for row in rows:
        sev = row.get("severity", "?")
        rid = row.get("rule_id", "?")
        msg = row.get("message", "")
        loc = ""
        if row.get("src"):
            src = row["src"]
            loc = f"  ({src.get('source_file', '')}:{src.get('line_number', 0)})"
        click.echo(f"  [{sev}] {rid}: {msg}{loc}")
    if fail_on_error and n_err > 0:
        sys.exit(1)


@daemon_cmd.group("ingest")
def ingest_group() -> None:
    """External-source ingestors (`gp daemon ingest adr|github`)."""


@ingest_group.command("adr")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def ingest_adr_cmd(folder: Path, repo: Path) -> None:
    """Ingest ADRs from a folder of Markdown files."""
    from ...daemon.ingestors import ingest_adr
    from ...runtime.store import Store, cache_path as _cache_path

    repo = repo.resolve()
    if not _cache_path(repo).exists():
        raise click.ClickException(
            f"no graph cache at {_cache_path(repo)} — run `gp init --repo {repo}` first"
        )
    store = Store(_cache_path(repo))
    try:
        summary = ingest_adr(store, repo, folder)
    finally:
        store.close()
    click.echo(
        f"ingested {summary['files']} ADR(s), "
        f"{summary['with_refs']} link to code symbols"
    )
    client = DaemonClient(repo)
    if client.is_running():
        client.call("refresh")


@ingest_group.command("github")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--state", default="all", help="open|closed|all (default: all).")
@click.option("--limit", default=200, type=int)
@click.option("--issues/--no-issues", default=True)
@click.option("--prs/--no-prs", default=True)
def ingest_github_cmd(repo: Path, state: str, limit: int, issues: bool, prs: bool) -> None:
    """Ingest GitHub issues + PRs via `gh` (must be authenticated)."""
    from ...daemon.ingestors import ingest_github
    from ...runtime.store import Store, cache_path as _cache_path

    repo = repo.resolve()
    if not _cache_path(repo).exists():
        raise click.ClickException(
            f"no graph cache at {_cache_path(repo)} — run `gp init --repo {repo}` first"
        )
    store = Store(_cache_path(repo))
    try:
        summary = ingest_github(
            store, repo, issues=issues, prs=prs, state=state, limit=limit
        )
    finally:
        store.close()
    click.echo(
        f"ingested {summary['issues']} issue(s) and {summary['prs']} PR(s)"
    )
    client = DaemonClient(repo)
    if client.is_running():
        client.call("refresh")


@daemon_cmd.group("claude-md")
def claude_md_group() -> None:
    """Manage the graphify-plus owned section in CLAUDE.md / AGENTS.md (Layer 23)."""


@claude_md_group.command("update")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--focus", default="", help="Per-task hint: focus the section on this symbol.")
def claude_md_update_cmd(repo: Path, focus: str) -> None:
    """Refresh the graphify-plus owned section across CLAUDE.md/AGENTS.md."""
    from ...daemon.context_file import build_section, render_section, update_all_known
    from ...daemon.indexes import InMemoryGraph
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    if not _cp(repo).exists():
        raise click.ClickException(f"no graph cache at {_cp(repo)}")
    store = Store(_cp(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    section = build_section(snap, focus=focus)
    body = render_section(section, repo_name=repo.name)
    results = update_all_known(repo, body)
    for r in results:
        click.echo(f"{r['action']}: {r['path']}")


@claude_md_group.command("strip")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def claude_md_strip_cmd(repo: Path) -> None:
    """Remove the graphify-plus owned section from CLAUDE.md."""
    from ...daemon.context_file import strip_owned_section

    repo = repo.resolve()
    p = repo / "CLAUDE.md"
    if not p.exists():
        click.echo("CLAUDE.md not found")
        return
    p.write_text(strip_owned_section(p.read_text(encoding="utf-8")), encoding="utf-8")
    click.echo(f"stripped owned section from {p}")


@daemon_cmd.group("plugin")
def plugin_group() -> None:
    """Manage third-party plugins (`gp daemon plugin list`)."""


@plugin_group.command("list")
@click.option("--json", "as_json", is_flag=True)
def plugin_list_cmd(as_json: bool) -> None:
    """List discovered plugins and what they registered."""
    from ...daemon.plugins import get_registry

    reg = get_registry()
    body: dict[str, Any] = {
        "plugins": list(reg.metadata.keys()),
        "handlers": list(reg.handlers.keys()),
        "ingestors": list(reg.ingestors.keys()),
    }
    if as_json:
        click.echo(json.dumps(body, indent=2))
        return
    if not body["plugins"]:
        click.echo("no plugins installed")
        click.echo(
            "  declare an entry point under "
            f"`graphify_plus.plugins` to register handlers/ingestors"
        )
        return
    for name in body["plugins"]:
        click.echo(f"  {name}")
        for k in reg.metadata.get(name, {}):
            click.echo(f"    - {k}")


@daemon_cmd.group("refactor")
def refactor_group() -> None:
    """Pre-built refactor playbooks (Layer 10.3)."""


@refactor_group.command("extract-module")
@click.option("--pattern", required=True)
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def refactor_extract_cmd(pattern: str, repo: Path, as_json: bool) -> None:
    from ...daemon.indexes import InMemoryGraph
    from ...daemon.workflows import extract_module_plan
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    store = Store(_cp(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    plan = extract_module_plan(snap, pattern)
    if as_json:
        click.echo(json.dumps(plan.to_dict(), indent=2))
    else:
        click.echo(f"# {plan.name}\n_{plan.description}_\n\nrisk={plan.risk}, changes={plan.estimated_changes}")
        for s in plan.steps:
            click.echo(f"  - {s.description}")


@refactor_group.command("rename")
@click.option("--node", required=True)
@click.option("--to", "new_name", required=True)
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def refactor_rename_cmd(node: str, new_name: str, repo: Path, as_json: bool) -> None:
    from ...daemon.indexes import InMemoryGraph
    from ...daemon.workflows import rename_plan
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    store = Store(_cp(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    plan = rename_plan(snap, node=node, to=new_name)
    if as_json:
        click.echo(json.dumps(plan.to_dict(), indent=2))
        return
    click.echo(f"# {plan.name}\nrisk={plan.risk}")
    for s in plan.steps:
        loc = f" ({s.file}:{s.line})" if s.file else ""
        click.echo(f"  - {s.description}{loc}")


@daemon_cmd.command("docs")
@click.argument("module")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--out", default="", help="Write to file instead of stdout.")
def docs_cmd(module: str, repo: Path, out: str) -> None:
    """Generate per-module documentation (Layer 10.5)."""
    from ...daemon.indexes import InMemoryGraph
    from ...daemon.workflows import generate_module_docs
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    store = Store(_cp(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    body = generate_module_docs(snap, module)
    if out:
        Path(out).write_text(body, encoding="utf-8")
        click.echo(f"wrote {out}")
    else:
        click.echo(body)


@daemon_cmd.command("time-machine")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--rev", default="", help="Show summary at this commit.")
@click.option("--compare", default="", help="<rev_a>..<rev_b> structural diff.")
@click.option("--evolution", "evolution_path", default="", help="Show commit history for this file.")
@click.option("--json", "as_json", is_flag=True)
def time_machine_cmd(
    repo: Path, rev: str, compare: str, evolution_path: str, as_json: bool
) -> None:
    """Time-machine queries (Layer 10.4)."""
    from ...daemon.workflows import at_revision, compare_revs, evolution

    repo = repo.resolve()
    if rev:
        body = at_revision(repo, rev)
    elif compare and ".." in compare:
        a, b = compare.split("..", 1)
        delta = compare_revs(repo, a, b)
        body = delta.__dict__
    elif evolution_path:
        body = evolution(repo, evolution_path)
    else:
        raise click.ClickException("specify --rev, --compare A..B, or --evolution PATH")
    if as_json:
        click.echo(json.dumps(body, indent=2, default=str))
    else:
        click.echo(json.dumps(body, indent=2, default=str))


@daemon_cmd.group("workspace")
def workspace_group() -> None:
    """Multi-repo / monorepo workspaces (Layer 11.3)."""


@workspace_group.command("list")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def workspace_list_cmd(repo: Path) -> None:
    from ...daemon.workflows import load_workspaces

    repo = repo.resolve()
    workspaces = load_workspaces(repo)
    if not workspaces:
        click.echo("no workspaces declared (.graphify_plus/workspaces.yaml)")
        return
    for w in workspaces:
        click.echo(f"  {w.name:<20} {w.path}  {w.description}")


@daemon_cmd.group("skill")
def skill_group() -> None:
    """Skills marketplace (Layer 11.4)."""


@skill_group.command("list")
@click.option("--registry", default="", help="Path to a local registry JSON.")
def skill_list_cmd(registry: str) -> None:
    from ...daemon.workflows import load_registry

    p = Path(registry) if registry else None
    rows = load_registry(p)
    for r in rows:
        click.echo(f"  {r.name:<20} {r.description}")
        click.echo(f"      {r.url}")


@skill_group.command("install")
@click.argument("name")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--registry", default="")
def skill_install_cmd(name: str, repo: Path, registry: str) -> None:
    from ...daemon.workflows import install_skill, load_registry

    p = Path(registry) if registry else None
    rows = load_registry(p)
    entry = next((r for r in rows if r.name == name), None)
    if entry is None:
        raise click.ClickException(f"no skill named {name!r} in registry")
    target = install_skill(repo.resolve(), entry)
    click.echo(f"installed: {target}")


@daemon_cmd.command("ingest-conversations")
@click.argument("transcripts_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def ingest_conversations_cmd(transcripts_dir: Path, repo: Path) -> None:
    """Ingest conversation transcripts as decision nodes (Layer 6.4)."""
    from ...daemon.workflows import ingest_conversations
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    store = Store(_cp(repo))
    try:
        summary = ingest_conversations(store, repo, transcripts_dir)
    finally:
        store.close()
    click.echo(
        f"ingested {summary['decisions']} decision(s) from {summary['transcripts']} transcript(s)"
    )


@daemon_cmd.command("license-audit")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def license_audit_cmd(repo: Path, as_json: bool) -> None:
    """License compatibility audit (Layer 9.3)."""
    from ...daemon.iac_config_license import license_audit

    repo = repo.resolve()
    out = license_audit(repo)
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    for p in out["packages"]:
        click.echo(f"  {p['package']}: {p['license']}  ({p['source']})")
    if out["issues"]:
        click.echo("")
        click.echo("issues:")
        for i in out["issues"]:
            click.echo(f"  ⚠ {i['package']} ({i['license']}): {i['issue']}")


@daemon_cmd.command("config-drift")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def config_drift_cmd(repo: Path, as_json: bool) -> None:
    """Env-var / feature-flag drift detection (Layer 8.4)."""
    from ...daemon.iac_config_license import detect_drift

    out = detect_drift(repo.resolve())
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    if out["referenced_but_undeclared"]:
        click.echo("env vars referenced in code but undeclared:")
        for k in out["referenced_but_undeclared"]:
            click.echo(f"  - {k}")
    if out["declared_but_unused"]:
        click.echo("env vars declared but unused:")
        for k in out["declared_but_unused"]:
            click.echo(f"  - {k}")
    if out["feature_flags"]:
        click.echo("feature flags referenced:")
        for k in out["feature_flags"]:
            click.echo(f"  - {k}")


@daemon_cmd.command("ingest-runtime")
@click.argument("report", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def ingest_runtime_cmd(report: Path, repo: Path) -> None:
    """Ingest a runtime trace (py-spy speedscope or pprof text)."""
    from ...daemon.runtime_intel import ingest_runtime
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    store = Store(_cp(repo))
    try:
        summary = ingest_runtime(store, repo, report)
    finally:
        store.close()
    click.echo(
        f"format={summary['format']}  raw_frames={summary['raw_frames']}  "
        f"attributed={summary['attributed']}"
    )


@daemon_cmd.command("find-origin")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--stacktrace", required=True, help="File containing the stacktrace.")
@click.option("--deploy-sha", default="", help="Deploy SHA for since-then movement check.")
@click.option("--json", "as_json", is_flag=True)
def find_origin_cmd(repo: Path, stacktrace: str, deploy_sha: str, as_json: bool) -> None:
    """Map a stacktrace to current code (Layer 7.3)."""
    from ...daemon.runtime_intel import map_stacktrace_to_symbols, parse_stacktrace
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    text = Path(stacktrace).read_text(encoding="utf-8")
    frames = parse_stacktrace(text)
    store = Store(_cp(repo))
    try:
        symbols = store.all_symbols()
    finally:
        store.close()
    mappings = map_stacktrace_to_symbols(frames, symbols, repo, deploy_sha=deploy_sha)
    rows = [
        {
            "file": m.frame.file,
            "line": m.frame.line,
            "func": m.frame.func,
            "symbol": m.label,
            "moved_since_deploy": m.moved_since_deploy,
            "edits_since_deploy": m.edits_since_deploy,
        }
        for m in mappings
    ]
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        loc = f"{r['file']}:{r['line']}"
        sym = f" → {r['symbol']}" if r['symbol'] else " (unmapped)"
        moved = " (moved)" if r["moved_since_deploy"] else ""
        click.echo(f"  {loc}{sym}{moved}  edits={r['edits_since_deploy']}")


@daemon_cmd.group("team")
def team_group() -> None:
    """Shared team graph (Layer 11.1, local-fs tier)."""


@team_group.command("init")
@click.argument("root", type=click.Path(path_type=Path))
@click.option("--name", default="team")
def team_init_cmd(root: Path, name: str) -> None:
    from ...daemon.team import init_team

    cfg = init_team(root, name=name)
    click.echo(f"team initialised at {cfg.root} (name={cfg.name})")


@team_group.command("push-annotation")
@click.argument("target")
@click.argument("note")
@click.option("--root", default="", help="Team root (default: $GRAPHIFY_PLUS_TEAM_ROOT).")
@click.option("--author", default="")
def team_push_cmd(target: str, note: str, root: str, author: str) -> None:
    import os

    from ...daemon.team import detect_team_root, push_annotation

    base = Path(root) if root else detect_team_root()
    if base is None:
        raise click.ClickException(
            "no team root; pass --root or set GRAPHIFY_PLUS_TEAM_ROOT"
        )
    out = push_annotation(
        base, target=target, note=note, author=author or os.environ.get("USER", "anon")
    )
    click.echo(f"pushed: {out['fingerprint']}")


@team_group.command("pull")
@click.option("--root", default="")
@click.option("--json", "as_json", is_flag=True)
def team_pull_cmd(root: str, as_json: bool) -> None:
    from ...daemon.team import detect_team_root, pull_annotations

    base = Path(root) if root else detect_team_root()
    if base is None:
        raise click.ClickException("no team root")
    rows = pull_annotations(base)
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("no annotations")
        return
    for r in rows:
        click.echo(
            f"  [{r.get('ts', '?')[:19]}] {r.get('author', '?')} → {r.get('target', '?')}: {r.get('note', '')[:80]}"
        )


@team_group.command("conflicts")
@click.option("--root", default="")
@click.option("--json", "as_json", is_flag=True)
def team_conflicts_cmd(root: str, as_json: bool) -> None:
    from ...daemon.team import detect_conflicts, detect_team_root, pull_annotations

    base = Path(root) if root else detect_team_root()
    if base is None:
        raise click.ClickException("no team root")
    rows = detect_conflicts(pull_annotations(base))
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("no conflicts")
        return
    for c in rows:
        click.echo(f"  {c['target']}: {c['n_distinct_notes']} distinct notes")


@daemon_cmd.group("writeback")
def writeback_group() -> None:
    """Session-scoped writeback proposals (Layer 4.2)."""


@writeback_group.command("propose")
@click.argument("target")
@click.argument("rationale")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def writeback_propose_cmd(target: str, rationale: str, repo: Path) -> None:
    from ...daemon.writeback_proposals import propose

    p = propose(repo.resolve(), target=target, rationale=rationale)
    click.echo(f"proposal {p.id} ({p.state})")


@writeback_group.command("list")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--state", default="", help="Filter: pending|accepted|rejected.")
def writeback_list_cmd(repo: Path, state: str) -> None:
    from ...daemon.writeback_proposals import list_proposals

    rows = list_proposals(repo.resolve(), state=state or None)
    if not rows:
        click.echo("no proposals")
        return
    for r in rows:
        click.echo(f"  {r.id}  {r.state:<9}  {r.target}: {r.rationale[:80]}")


@writeback_group.command("decide")
@click.argument("proposal_id")
@click.option("--accept/--reject", default=True)
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def writeback_decide_cmd(proposal_id: str, accept: bool, repo: Path) -> None:
    from ...daemon.writeback_proposals import decide

    out = decide(repo.resolve(), proposal_id, accept=accept)
    if out is None:
        raise click.ClickException(f"no proposal with id {proposal_id}")
    click.echo(f"{out.id} → {out.state}")


@writeback_group.command("auto-accept")
@click.option("--enable/--disable", default=True)
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def writeback_auto_accept_cmd(enable: bool, repo: Path) -> None:
    from ...daemon.writeback_proposals import disable_auto_accept, enable_auto_accept

    if enable:
        s = enable_auto_accept(repo.resolve())
    else:
        s = disable_auto_accept(repo.resolve())
    click.echo(f"auto_accept = {s.auto_accept}")


@daemon_cmd.command("correctness-stats")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def correctness_stats_cmd(repo: Path, as_json: bool) -> None:
    """Continuous correctness sampling report (Layer 14.4)."""
    from ...daemon.correctness_sampling import disagreement_rate

    out = disagreement_rate(repo.resolve())
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"samples: {out['samples']}  disagreement rate: {out.get('rate', 0.0):.1%}")
    for ext, stats in (out.get("by_extractor") or {}).items():
        click.echo(f"  {ext}: {stats['disagreed']}/{stats['samples']}")


@daemon_cmd.command("notify")
@click.argument("event")
@click.argument("title")
@click.argument("body")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def notify_cmd(event: str, title: str, body: str, repo: Path) -> None:
    """Dispatch a notification through the configured sinks (Layer 19)."""
    from ...daemon.notifications import dispatch

    out = dispatch(repo.resolve(), event=event, title=title, body=body)
    for entry in out:
        click.echo(json.dumps(entry))


@daemon_cmd.command("session-start")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def session_start_cmd(repo: Path, as_json: bool) -> None:
    """Start an always-on session: daemon + watcher + claude-md + health snapshot."""
    from ...daemon.session_manager import session_start

    out = session_start(repo.resolve())
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"session: {out['repo']}  started {out['started_at']}")
    for s in out["steps"]:
        if "error" in s:
            click.echo(f"  ✗ {s['step']}: {s['error']}")
        elif "skipped" in s:
            click.echo(f"  · {s['step']}: skipped ({s['skipped']})")
        else:
            click.echo(f"  ✓ {s['step']}")


@daemon_cmd.command("session-status")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def session_status_cmd(repo: Path, as_json: bool) -> None:
    """Single-command 'is the always-on environment healthy?'"""
    from ...daemon.session_manager import session_status

    state = session_status(repo.resolve())
    body = state.__dict__
    if as_json:
        click.echo(json.dumps(body, indent=2))
        return
    for k, v in body.items():
        click.echo(f"  {k:<20} {v}")


@daemon_cmd.command("pre-edit")
@click.argument("target")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def pre_edit_cmd(target: str, repo: Path, as_json: bool) -> None:
    """Run the master plan's 8-step pre-edit ritual against TARGET."""
    from ...daemon.indexes import InMemoryGraph
    from ...daemon.session_manager import pre_edit
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    store = Store(_cp(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    rep = pre_edit(snap, target)
    if as_json:
        click.echo(json.dumps(rep.to_dict(), indent=2))
        return
    click.echo(f"# pre-edit ritual — {target}")
    click.echo(f"risk: {rep.risk}")
    if rep.affected:
        click.echo("\naffected:")
        for a in rep.affected:
            click.echo(f"  - {a['label']}  {a['source_file']}:{a['line_number']}")
    if rep.blast_radius:
        click.echo(f"\nblast radius ({len(rep.blast_radius)} dependents):")
        for d in rep.blast_radius[:8]:
            click.echo(f"  - {d['label']}  {d['source_file']}:{d['line_number']}")
    if rep.rule_violations:
        click.echo(f"\nrule violations on this symbol: {len(rep.rule_violations)}")
    if rep.tests_to_run:
        click.echo("\ntests to run:")
        for t in rep.tests_to_run:
            click.echo(f"  - {t['label']}  {t['source_file']}:{t['line_number']}")
    if rep.coverage_pct is not None:
        click.echo(f"\ncoverage: {rep.coverage_pct}%")
    if rep.notes:
        click.echo("\nnotes:")
        for n in rep.notes:
            click.echo(f"  · {n}")


@daemon_cmd.command("changelog")
@click.option("--since", required=True, help="Git ref / tag the changelog starts from.")
@click.option("--head", default="HEAD")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def changelog_cmd(since: str, head: str, repo: Path, as_json: bool) -> None:
    """Auto-generate a Markdown changelog from conventional commits."""
    from ...daemon.changelog import make_changelog, render

    rep = make_changelog(repo.resolve(), since, head)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "since_ref": rep.since_ref,
                    "head_ref": rep.head_ref,
                    "by_type": {k: [c.__dict__ for c in v] for k, v in rep.by_type.items()},
                    "other": [c.__dict__ for c in rep.other],
                },
                indent=2,
            )
        )
        return
    click.echo(render(rep))


@daemon_cmd.command("audit-rebuild")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def audit_rebuild_cmd(repo: Path, as_json: bool) -> None:
    """Replay the audit log to rebuild the annotation/correction state (Layer 20.4)."""
    from ...daemon.audit_log import rebuild_from_log

    out = rebuild_from_log(repo.resolve())
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"records: {out['records_read']}  tombstoned: {out['tombstoned']}")
    click.echo(f"annotations to apply: {len(out['annotations_to_apply'])}")
    click.echo(f"corrections to apply: {len(out['corrections_to_apply'])}")
    click.echo(f"log hash: {out['log_hash']}")


@daemon_cmd.command("benchmark")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--corpus", default="", help="Corpus JSON; default = built-in tiny fixture.")
@click.option("--json", "as_json", is_flag=True)
def benchmark_cmd(repo: Path, corpus: str, as_json: bool) -> None:
    """Run graphify-plus against a benchmark corpus (Layer 14.3)."""
    from ...daemon.benchmark import (
        builtin_tiny_corpus,
        load_corpus,
        render_report,
        run_entry,
    )
    from ...daemon.indexes import InMemoryGraph
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    if corpus:
        entries = load_corpus(Path(corpus))
    else:
        entries = builtin_tiny_corpus()
    store = Store(_cp(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    reports = [run_entry(e, snap) for e in entries]
    body = {
        "reports": [
            {
                "name": r.name,
                "passed": r.passed,
                "total": r.total,
                "precision": r.precision(),
                "results": [c.__dict__ for c in r.results],
            }
            for r in reports
        ]
    }
    if as_json:
        click.echo(json.dumps(body, indent=2))
    else:
        click.echo(render_report(reports))


@daemon_cmd.command("quickstart")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def quickstart_cmd(repo: Path, as_json: bool) -> None:
    """First-run UX (Layer 5.3): init + snapshot + install + recipes."""
    from ...daemon.quickstart import render_quickstart, run_quickstart

    out = run_quickstart(repo)
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(render_quickstart(out))


@daemon_cmd.command("privacy")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option(
    "--dry-run-network",
    "dry_run",
    is_flag=True,
    help="List every outbound endpoint the current configuration would touch.",
)
@click.option("--json", "as_json", is_flag=True)
def privacy_cmd(repo: Path, dry_run: bool, as_json: bool) -> None:
    """Privacy / security model declaration (Layer 12.1)."""
    from ...daemon.ops_rigor import PRIVACY_DECLARATION, dry_run_network

    if dry_run:
        probes = dry_run_network(repo.resolve())
        if as_json:
            click.echo(json.dumps([p.__dict__ for p in probes], indent=2))
            return
        for p in probes:
            mark = "ENABLED" if p.enabled else "off"
            click.echo(f"  [{mark:<8}] {p.purpose:<22}  {p.endpoint}")
            if p.note:
                click.echo(f"             {p.note}")
        return
    click.echo(PRIVACY_DECLARATION)


@daemon_cmd.command("perfcheck")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--samples", default=500, type=int)
@click.option("--json", "as_json", is_flag=True)
def perfcheck_cmd(repo: Path, samples: int, as_json: bool) -> None:
    """Performance regression check (Layer 12.2)."""
    from ...daemon.indexes import InMemoryGraph
    from ...daemon.ops_rigor import perfcheck
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    if not _cp(repo).exists():
        raise click.ClickException(f"no graph cache; run `gp init` first")
    store = Store(_cp(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    res = perfcheck(snap, samples=samples)
    body = {
        "n_symbols": res.n_symbols,
        "samples": res.samples,
        "p50_ms": res.p50_ms,
        "p95_ms": res.p95_ms,
        "p99_ms": res.p99_ms,
        "target": res.target,
        "pass_p50": res.pass_p50,
        "pass_p99": res.pass_p99,
    }
    if as_json:
        click.echo(json.dumps(body, indent=2))
    else:
        click.echo(
            f"{res.n_symbols} symbols, {res.samples} samples — "
            f"p50={res.p50_ms}ms p95={res.p95_ms}ms p99={res.p99_ms}ms"
        )
        click.echo(
            f"target: p50<={res.target.get('p50_ms', '?')}ms p99<={res.target.get('p99_ms', '?')}ms"
        )
        click.echo(
            f"pass_p50={res.pass_p50}  pass_p99={res.pass_p99}"
        )
    if not (res.pass_p50 and res.pass_p99):
        sys.exit(1)


@daemon_cmd.command("tutorial")
@click.option("--json", "as_json", is_flag=True)
def tutorial_cmd(as_json: bool) -> None:
    """Print the 10-question graph-vs-grep tutorial (Layer 25)."""
    from ...daemon.tutorial import TUTORIAL_STEPS, render_tutorial

    if as_json:
        click.echo(json.dumps([s.__dict__ for s in TUTORIAL_STEPS], indent=2))
        return
    click.echo(render_tutorial())


@daemon_cmd.command("recipes")
@click.option("--install", is_flag=True, help="Install recipes into .graphify_plus/queries/.")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def recipes_cmd(install: bool, repo: Path, as_json: bool) -> None:
    """Show / install graphify-plus recipes (Layer 25)."""
    from ...daemon.tutorial import RECIPES, install_recipes, render_recipes

    if install:
        out = install_recipes(repo)
        for p in out:
            click.echo(f"installed: {p}")
        return
    if as_json:
        click.echo(json.dumps([r.__dict__ for r in RECIPES], indent=2))
        return
    click.echo(render_recipes())


@daemon_cmd.command("local-llm")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def local_llm_cmd(repo: Path, as_json: bool) -> None:
    """Probe local LLM backends + show routing (Layer 22)."""
    from ...daemon.local_llm import detect_backends, load_routing

    backends = detect_backends()
    routing = load_routing(repo.resolve())
    if as_json:
        click.echo(
            json.dumps(
                {
                    "backends": [b.__dict__ for b in backends],
                    "routing": {
                        "default": routing.default,
                        "rules": [r.__dict__ for r in routing.rules],
                        "llm_free": routing.llm_free,
                    },
                },
                indent=2,
            )
        )
        return
    for b in backends:
        status = "✓" if b.available else "✗"
        click.echo(f"  {status} {b.name:<10} {b.url or '(unconfigured)'}  {b.reason}")
    click.echo("")
    click.echo(f"routing.default={routing.default}, llm_free={routing.llm_free}")
    for r in routing.rules:
        click.echo(f"  {r.pattern} → {r.backend}")


@daemon_cmd.command("ingest-long-tail")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option(
    "--kinds", default="", help="Comma-separated subset (jupyter,openapi,k8s,…)."
)
def ingest_long_tail_cmd(repo: Path, kinds: str) -> None:
    """Ingest long-tail external sources (Layer 24)."""
    from ...daemon.long_tail_ingestors import ingest_long_tail
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    if not _cp(repo).exists():
        raise click.ClickException(f"no graph cache; run `gp init`")
    kinds_list = [k.strip() for k in kinds.split(",") if k.strip()] or None
    store = Store(_cp(repo))
    try:
        summary = ingest_long_tail(store, repo, kinds=kinds_list)
    finally:
        store.close()
    if not summary:
        click.echo("nothing ingested (no matching files found)")
        return
    for k, n in summary.items():
        click.echo(f"  {k:<12} {n}")


@daemon_cmd.command("version-check")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def version_check_cmd(repo: Path) -> None:
    """Check PyPI for a newer release (Layer 27)."""
    from graphify_plus import __version__
    from ...daemon.distribution import version_check

    info = version_check(repo.resolve(), __version__)
    if info.error == "disabled":
        click.echo("update check disabled (GP_NO_UPDATE_CHECK=1)")
        return
    click.echo(f"current: {info.current}")
    click.echo(f"latest:  {info.latest or '(unknown)'}")
    if info.update_available:
        click.echo(f"update available — `pip install -U graphify-plus`")
    elif info.error:
        click.echo(f"check failed: {info.error}")
    else:
        click.echo("up-to-date")


@daemon_cmd.command("trend")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--limit", default=90, type=int)
@click.option("--weekly", is_flag=True, help="Render the last 7 snapshots as a weekly digest.")
@click.option("--snapshot", is_flag=True, help="Take a fresh snapshot first.")
@click.option("--json", "as_json", is_flag=True)
def trend_cmd(repo: Path, limit: int, weekly: bool, snapshot: bool, as_json: bool) -> None:
    """Show health trend / weekly digest from .graphify_plus/health-history.jsonl."""
    from ...daemon.anomaly import (
        append_snapshot,
        history,
        render_trend,
        render_weekly,
        take_snapshot,
        trend_summary,
    )
    from ...daemon.indexes import InMemoryGraph
    from ...runtime.store import Store, cache_path as _cp

    repo = repo.resolve()
    if snapshot:
        if not _cp(repo).exists():
            raise click.ClickException("no graph cache; run `gp init` first")
        store = Store(_cp(repo))
        try:
            snap = InMemoryGraph.from_store(store, repo)
        finally:
            store.close()
        append_snapshot(repo, take_snapshot(snap))
        click.echo("snapshot appended")
    rows = history(repo, limit=limit)
    if as_json:
        click.echo(json.dumps({"summary": trend_summary(rows), "rows": rows}, indent=2))
        return
    if weekly:
        click.echo(render_weekly(repo, rows))
    else:
        click.echo(render_trend(rows))


@daemon_cmd.group("audit")
def audit_group() -> None:
    """Audit log + logical undo (Layer 20)."""


@audit_group.command("log")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--namespace", default="", help="Filter by namespace (default: current branch).")
@click.option("--json", "as_json", is_flag=True)
def audit_log_cmd(repo: Path, namespace: str, as_json: bool) -> None:
    from ...daemon.audit_log import current_namespace, read_all

    repo = repo.resolve()
    ns = namespace or current_namespace(repo)
    rows = [r for r in read_all(repo) if not namespace or r.namespace == ns]
    if as_json:
        click.echo(json.dumps([r.to_dict() for r in rows], indent=2))
        return
    if not rows:
        click.echo("no audit records")
        return
    for r in rows:
        click.echo(
            f"  {r.id}  {r.kind:<12}  {r.namespace}  {r.target}  {r.agent}"
        )


@audit_group.command("revert")
@click.argument("record_id")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--agent", default="cli")
def audit_revert_cmd(record_id: str, repo: Path, agent: str) -> None:
    from ...daemon.audit_log import revert

    repo = repo.resolve()
    rec = revert(repo, record_id, agent=agent)
    click.echo(f"tombstone written: {rec.id} -> superseded {record_id}")


@daemon_cmd.command("lsp")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def lsp_cmd(repo: Path) -> None:
    """Run the LSP shim over stdio (Layer 17)."""
    from ...daemon.lsp_shim import run_lsp

    sys.exit(run_lsp(repo.resolve()))


@daemon_cmd.command("gpl")
@click.argument("query", required=False, default="")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--nl", default="", help="Natural-language query; translated to GPL.")
@click.option("--save", default="", help="Save the query under this name.")
@click.option("--run", default="", help="Run a previously-saved query by name.")
@click.option("--json", "as_json", is_flag=True)
def gpl_cmd(
    query: str, repo: Path, nl: str, save: str, run: str, as_json: bool
) -> None:
    """Run a GPL query (Layer 16).

    Examples:
      gp daemon gpl "FIND nodes WHERE test_coverage < 0.1"
      gp daemon gpl --nl "untested public api functions"
      gp daemon gpl --save untested-funcs "FIND nodes WHERE test_coverage < 0.1"
      gp daemon gpl --run untested-funcs
    """
    repo = repo.resolve()
    saved_dir = repo / ".graphify_plus" / "queries"
    if save and query:
        saved_dir.mkdir(parents=True, exist_ok=True)
        (saved_dir / f"{save}.gpl").write_text(query, encoding="utf-8")
        click.echo(f"saved query: {saved_dir / (save + '.gpl')}")
        return
    if run:
        path = saved_dir / f"{run}.gpl"
        if not path.exists():
            raise click.ClickException(f"no saved query named {run!r}")
        query = path.read_text(encoding="utf-8")
    args: dict[str, Any] = {}
    if query:
        args["query"] = query
    if nl:
        args["nl"] = nl
    payload = _route_intent(repo, "gpl_query", args)
    if "error" in payload:
        raise click.ClickException(payload["error"]["message"])
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    extra = payload.get("extra", {})
    if "nl_to_gpl" in extra:
        click.echo(f"# translated: {extra['nl_to_gpl']}")
    click.echo(f"# {extra.get('count', 0)} match(es)")
    for row in payload.get("results", []):
        click.echo("  " + "  ".join(f"{k}={v!r}" for k, v in row.items()))


@daemon_cmd.command("cross-stack")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--rebuild", is_flag=True, help="Re-detect cross-stack edges first.")
@click.option("--node", default="", help="Show edges for a specific symbol.")
@click.option("--json", "as_json", is_flag=True)
def cross_stack_cmd(repo: Path, rebuild: bool, node: str, as_json: bool) -> None:
    """HTTP boundary + DB schema edges (Layer 8).

    With ``--rebuild``, re-runs detection across all source files and
    persists. Without arguments, prints repo-wide stats. With
    ``--node``, prints inbound + outbound cross edges for the symbol.
    """
    from ...runtime.store import Store, cache_path as _cache_path

    repo = repo.resolve()
    if not _cache_path(repo).exists():
        raise click.ClickException(
            f"no graph cache at {_cache_path(repo)} — run `gp init --repo {repo}` first"
        )
    if rebuild:
        from ...daemon.cross_stack import synthesise

        store = Store(_cache_path(repo))
        try:
            summary = synthesise(store, repo)
        finally:
            store.close()
        click.echo(
            f"detected {summary['http_backends']} backend(s), "
            f"{summary['http_callers']} caller(s) → {summary['http_edges']} HTTP edge(s); "
            f"{summary['db_tables']} table(s) → {summary['db_edges']} DB edge(s)"
        )
        client = DaemonClient(repo)
        if client.is_running():
            client.call("refresh")
        return

    args: dict[str, Any] = {}
    if node:
        args["node"] = node
    payload = _route_intent(repo, "cross_stack", args)
    if "error" in payload:
        raise click.ClickException(payload["error"]["message"])
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    extra = payload.get("extra", {})
    if not node:
        click.echo(f"# {extra.get('total', 0)} cross-stack edge(s)")
        for kind, n in (extra.get("by_kind") or {}).items():
            click.echo(f"  {kind:<10} {n}")
        return
    rows = payload.get("results", [])
    if not rows:
        click.echo(extra.get("reason", "no cross-stack edges"))
        return
    for row in rows:
        d = row.get("detail") or {}
        click.echo(
            f"  [{row.get('kind', '?')}] {row.get('direction', '?'):<3} "
            f"{row.get('src', '?')} → {row.get('dst', '?')}"
        )
        for k, v in d.items():
            click.echo(f"      {k}: {v}")


@daemon_cmd.group("security")
def security_group() -> None:
    """CVE + SAST overlays (`gp daemon security ingest|ingest-sast|vulnerable|risky`)."""


@security_group.command("ingest")
@click.argument("report", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def security_ingest_audit_cmd(report: Path, repo: Path) -> None:
    """Ingest a pip-audit / npm-audit JSON report (CVE overlay)."""
    from ...daemon.overlays import ingest_audit
    from ...runtime.store import Store, cache_path as _cache_path

    repo = repo.resolve()
    if not _cache_path(repo).exists():
        raise click.ClickException(
            f"no graph cache at {_cache_path(repo)} — run `gp init --repo {repo}` first"
        )
    store = Store(_cache_path(repo))
    try:
        summary = ingest_audit(store, repo, report)
    finally:
        store.close()
    click.echo(
        f"ingested {report.name} ({summary['format']}): "
        f"{summary['cves']} CVE(s), {summary['packages']} package(s)"
    )
    client = DaemonClient(repo)
    if client.is_running():
        client.call("refresh")


@security_group.command("ingest-sast")
@click.argument("report", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def security_ingest_sast_cmd(report: Path, repo: Path) -> None:
    """Ingest a Bandit / Semgrep JSON report (SAST overlay)."""
    from ...daemon.overlays import ingest_sast
    from ...runtime.store import Store, cache_path as _cache_path

    repo = repo.resolve()
    if not _cache_path(repo).exists():
        raise click.ClickException(
            f"no graph cache at {_cache_path(repo)} — run `gp init --repo {repo}` first"
        )
    store = Store(_cache_path(repo))
    try:
        summary = ingest_sast(store, repo, report)
    finally:
        store.close()
    by_sev = ", ".join(f"{k}={v}" for k, v in summary["by_severity"].items())
    click.echo(
        f"ingested {report.name} ({summary['format']}): "
        f"{summary['findings']} finding(s), {summary['attributed']} attributed "
        f"({by_sev})"
    )
    client = DaemonClient(repo)
    if client.is_running():
        client.call("refresh")


@security_group.command("vulnerable")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--severity", default="", help="Floor: CRITICAL/HIGH/MEDIUM/LOW (default: all).")
@click.option("--json", "as_json", is_flag=True)
def security_vulnerable_cmd(repo: Path, severity: str, as_json: bool) -> None:
    """List symbols reachable from CVE-affected packages."""
    repo = repo.resolve()
    payload = _route_intent(
        repo, "whats_vulnerable", {"severity": severity, "budget_tokens": 4000}
    )
    if "error" in payload:
        raise click.ClickException(payload["error"]["message"])
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    rows = payload.get("results", [])
    extra = payload.get("extra", {})
    if not rows:
        click.echo(extra.get("reason", "no reachable CVEs"))
        return
    click.echo(
        f"# {len(rows)} symbol(s) reach {extra.get('total_cves', 0)} CVE(s) "
        f"across {extra.get('reachable_symbols', 0)} package import path(s)"
    )
    for row in rows:
        cves = row.get("vulnerabilities") or []
        click.echo(
            f"  [{row.get('worst_severity', '?'):<8}] {row.get('label', '?')}  "
            f"{row.get('source_file', '?')}:{row.get('line_number', 0)}"
        )
        for c in cves[:3]:
            click.echo(
                f"    - {c.get('cve_id', '?')} in {c.get('package', '?')} "
                f"{c.get('version', '?')}: {c.get('summary', '')[:120]}"
            )


@security_group.command("risky")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--severity", default="", help="Floor: HIGH/MEDIUM/LOW (default: all).")
@click.option("--json", "as_json", is_flag=True)
def security_risky_cmd(repo: Path, severity: str, as_json: bool) -> None:
    """List symbols with attached SAST findings."""
    repo = repo.resolve()
    payload = _route_intent(
        repo, "whats_risky", {"severity": severity, "budget_tokens": 4000}
    )
    if "error" in payload:
        raise click.ClickException(payload["error"]["message"])
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    rows = payload.get("results", [])
    if not rows:
        click.echo(payload.get("extra", {}).get("reason", "no risky symbols"))
        return
    for row in rows:
        click.echo(
            f"  [{row.get('worst_severity', '?'):<8}] {row.get('label', '?')}  "
            f"{row.get('source_file', '?')}:{row.get('line_number', 0)}  "
            f"({row.get('n_findings', 0)} finding(s))"
        )
        for f in (row.get("sast_findings") or [])[:3]:
            click.echo(
                f"    - {f.get('rule_id', '?')}: {f.get('message', '')[:120]}"
            )


@daemon_cmd.command("session-digest")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--since", default="main", help="Git ref the session started from.")
@click.option("--head", default="HEAD", help="Git ref the session ended at.")
@click.option("--json", "as_json", is_flag=True)
def session_digest_cmd(repo: Path, since: str, head: str, as_json: bool) -> None:
    """End-of-session summary suitable for pasting into a PR description.

    Combines what changed, what rules you bent, what's now untested,
    and recommended next steps into one Markdown block.
    """
    from ...daemon.review import Review, TouchedNode
    from ...daemon.session_digest import SessionDigest, format_digest

    repo = repo.resolve()
    payload = _route_intent(repo, "session_digest", {"since": since, "head": head})
    if "error" in payload:
        raise click.ClickException(payload["error"]["message"])
    body = payload.get("extra", {}).get("digest")
    if not body:
        raise click.ClickException("session-digest returned no payload")
    if as_json:
        click.echo(json.dumps(body, indent=2))
        return
    review = None
    if body.get("review"):
        rb = body["review"]
        review = Review(
            base=rb["base"],
            head=rb["head"],
            files_changed=rb["files_changed"],
            files_renamed=rb["files_renamed"],
            touched=[TouchedNode(**n) for n in rb["touched"]],
            untested_touched=[TouchedNode(**n) for n in rb["untested_touched"]],
            central_touched=[TouchedNode(**n) for n in rb["central_touched"]],
            rules_violations=list(rb["rules_violations"]),
            rules_grade=rb["rules_grade"],
            blast_radius=[TouchedNode(**n) for n in rb["blast_radius"]],
            summary=rb["summary"],
        )
    digest = SessionDigest(
        repo=body["repo"],
        since_ref=body["since_ref"],
        head_ref=body["head_ref"],
        started_at=body.get("started_at", ""),
        ended_at=body.get("ended_at", ""),
        review=review,
        coverage_overall_pct=body.get("coverage_overall_pct"),
        next_steps=list(body.get("next_steps", [])),
        rules_grade=body.get("rules_grade", "A"),
        rules_violations_count=int(body.get("rules_violations_count", 0)),
    )
    click.echo(format_digest(digest))


@daemon_cmd.command("diagnose")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True)
def diagnose_cmd(repo: Path, as_json: bool) -> None:
    """One-screen operational status: daemon, cache, snapshot, coverage,
    rules, telemetry, process. Designed to answer "is graphify-plus
    broken?" in a single command.
    """
    from ...daemon.diagnose import diagnose, format_diagnose

    repo = repo.resolve()
    report = diagnose(repo)
    if as_json:
        click.echo(json.dumps(report, indent=2))
        return
    click.echo(format_diagnose(report))


@daemon_cmd.command("onboard")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--persona", default="engineer", help="Audience for the tour.")
@click.option("--json", "as_json", is_flag=True)
def onboard_cmd(repo: Path, persona: str, as_json: bool) -> None:
    """Generate an onboarding walkthrough for a new contributor.

    Renders top central nodes, well-tested exemplars, and per-module
    starting points as a single Markdown document. Pipe into a wiki page
    or paste into a CONTRIBUTING.md.
    """
    from ...daemon.onboarding import OnboardingPlan, WalkStop, format_plan

    repo = repo.resolve()
    payload = _route_intent(repo, "onboard", {"persona": persona})
    if "error" in payload:
        raise click.ClickException(payload["error"]["message"])
    body = payload.get("extra", {}).get("onboarding")
    if not body:
        raise click.ClickException("onboard returned no payload")
    if as_json:
        click.echo(json.dumps(body, indent=2))
        return
    plan = OnboardingPlan(
        repo=body.get("repo", repo.name),
        persona=body.get("persona", persona),
        n_symbols=int(body.get("n_symbols", 0)),
        n_files=int(body.get("n_files", 0)),
        central=[WalkStop(**s) for s in body.get("central", [])],
        welltested_examples=[WalkStop(**s) for s in body.get("welltested_examples", [])],
        by_module={
            k: [WalkStop(**s) for s in v] for k, v in (body.get("by_module") or {}).items()
        },
    )
    click.echo(format_plan(plan))


@daemon_cmd.command("review")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--base", default="main", help="Git ref to compare against.")
@click.option("--head", default="HEAD", help="Git ref to review.")
@click.option("--json", "as_json", is_flag=True)
def review_cmd(repo: Path, base: str, head: str, as_json: bool) -> None:
    """PR review co-pilot — symbols touched, central-touched, untested touched,
    rules violations, blast radius. Markdown by default; ``--json`` for tools.
    """
    from ...daemon.review import (
        Review,
        TouchedNode,
        format_review,
    )

    repo = repo.resolve()
    payload = _route_intent(repo, "review", {"base": base, "head": head})
    if "error" in payload:
        raise click.ClickException(payload["error"]["message"])
    body = payload.get("extra", {}).get("review")
    if not body:
        raise click.ClickException("review returned no payload")
    if as_json:
        click.echo(json.dumps(body, indent=2))
        return
    rev = Review(
        base=body.get("base", base),
        head=body.get("head", head),
        files_changed=body.get("files_changed", 0),
        files_renamed=body.get("files_renamed", 0),
        touched=[TouchedNode(**n) for n in body.get("touched", [])],
        untested_touched=[TouchedNode(**n) for n in body.get("untested_touched", [])],
        central_touched=[TouchedNode(**n) for n in body.get("central_touched", [])],
        rules_violations=list(body.get("rules_violations", [])),
        rules_grade=body.get("rules_grade", "A"),
        blast_radius=[TouchedNode(**n) for n in body.get("blast_radius", [])],
        summary=body.get("summary", ""),
    )
    click.echo(format_review(rev))


@daemon_cmd.command("plan")
@click.argument("task")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--top-k", default=12, type=int, help="Number of affected-node candidates.")
def plan_cmd(task: str, repo: Path, as_json: bool, top_k: int) -> None:
    """Graph-grounded edit plan for a natural-language TASK description.

    Routes through the running daemon when available; falls back to a
    direct in-process build otherwise. Output is a Markdown report
    with affected nodes (with file:line), blast radius, risk grade, and
    a graph-vs-grep token-cost estimate.
    """
    from ...daemon.planner import Plan, PlanNode, format_plan

    repo = repo.resolve()
    client = DaemonClient(repo)
    payload: dict[str, Any] | None = None
    if client.is_running():
        try:
            resp = client.call("plan", {"task": task, "top_k": top_k})
            payload = resp.get("extra", {}).get("plan")
        except DaemonError as exc:
            raise click.ClickException(f"{exc.code}: {exc}") from exc
    if payload is None:
        # Direct fallback.
        from ...daemon.handlers import plan as plan_handler
        from ...daemon.indexes import InMemoryGraph
        from ...runtime.store import Store, cache_path as _cache_path

        store = Store(_cache_path(repo))
        try:
            snap = InMemoryGraph.from_store(store, repo)
        finally:
            store.close()
        result = plan_handler(snap, {"task": task, "top_k": top_k})
        payload = result.get("extra", {}).get("plan")
    if not payload:
        raise click.ClickException("plan generation returned no payload")

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    plan_obj = Plan(
        task=payload.get("task", task),
        affected=[PlanNode(**n) for n in payload.get("affected", [])],
        blast_radius=[PlanNode(**n) for n in payload.get("blast_radius", [])],
        risk=payload.get("risk", "LOW"),
        risk_reasons=list(payload.get("risk_reasons", [])),
        estimated_tokens_graph=int(payload.get("estimated_tokens_graph", 0)),
        estimated_tokens_grep=int(payload.get("estimated_tokens_grep", 0)),
        notes=list(payload.get("notes", [])),
    )
    click.echo(format_plan(plan_obj))


@daemon_cmd.command("stats")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def stats_cmd(repo: Path, as_json: bool) -> None:
    """Show local telemetry: per-op call counts, P50/P95 latency, FRESH rate.

    Reads ``<repo>/.graphify_plus/telemetry.jsonl`` — written by the
    daemon on every successful call. Local-only by default; the existing
    ``GRAPHIFY_TELEMETRY_URL`` exporter handles outbound emissions.
    """
    from ...daemon.telemetry import aggregate

    repo = repo.resolve()
    summary = aggregate(repo)
    if as_json:
        click.echo(json.dumps(summary, indent=2))
        return
    if summary["total_calls"] == 0:
        click.echo("no telemetry yet — run a few `gp daemon query …` calls first")
        return
    click.echo(f"daemon stats ({repo})")
    click.echo(f"  total calls : {summary['total_calls']}")
    click.echo(f"  ok rate     : {summary['ok_rate']:.1%}")
    click.echo(f"  FRESH rate  : {summary['fresh_rate']:.1%}")
    click.echo("")
    click.echo("  per-op:")
    for op, info in sorted(
        summary["by_op"].items(), key=lambda kv: -int(kv[1]["calls"])
    ):
        click.echo(
            f"    {op:<24} calls={info['calls']:>5}  "
            f"p50={info['p50_ms']}ms  p95={info['p95_ms']}ms  "
            f"FRESH={info['fresh_rate']:.0%}  "
            f"avg_results={info['avg_results']}"
        )
    if "top_errors" in summary:
        click.echo("")
        click.echo("  top errors:")
        for code, n in summary["top_errors"]:
            click.echo(f"    {code:<24} {n}")


@daemon_cmd.command("install")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option(
    "--force", is_flag=True, help="Overwrite existing files instead of skipping."
)
def install_cmd(repo: Path, force: bool) -> None:
    """Install the routing skill and the pre-grep hook into ``.claude/``.

    Drops two files into the repo:

      * ``.claude/skills/graphify-plus/SKILL.md`` — the routing rule
        Claude reads to decide *when* to use graph tools instead of grep.
      * ``.claude/hooks/pre_grep_hook.py`` — a PreToolUse hook that
        nudges Claude toward graphify-plus when grep is about to run on
        a bareword and the daemon has a structural hit.

    The hook is *not* auto-registered in ``settings.json`` — that's an
    explicit step the user takes when they want it on. We print the
    snippet so the operator can copy/paste.
    """
    import shutil
    from importlib import resources

    repo = repo.resolve()
    skill_dst = repo / ".claude" / "skills" / "graphify-plus" / "SKILL.md"
    hook_dst = repo / ".claude" / "hooks" / "pre_grep_hook.py"

    template_root = resources.files("graphify_plus.daemon.templates")
    written: list[Path] = []
    skipped: list[Path] = []
    for src_name, dst in (("SKILL.md", skill_dst), ("pre_grep_hook.py", hook_dst)):
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and not force:
            skipped.append(dst)
            continue
        with resources.as_file(template_root / src_name) as src_path:
            shutil.copyfile(src_path, dst)
        if dst.suffix == ".py":
            dst.chmod(0o755)
        written.append(dst)

    for p in written:
        click.echo(f"installed: {p.relative_to(repo)}")
    for p in skipped:
        click.echo(f"skipped (exists, pass --force): {p.relative_to(repo)}", err=True)
    click.echo("")
    click.echo("To enable the pre-grep hook, add this to .claude/settings.json:")
    click.echo(
        '  {\n'
        '    "hooks": {\n'
        '      "PreToolUse": [\n'
        '        {\n'
        '          "matcher": "Grep|Glob",\n'
        '          "hooks": [\n'
        '            {"type": "command", "command": ".claude/hooks/pre_grep_hook.py"}\n'
        '          ]\n'
        '        }\n'
        '      ]\n'
        '    }\n'
        '  }'
    )


@daemon_cmd.command("query")
@click.argument("op")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option(
    "-a",
    "--arg",
    "args_kv",
    multiple=True,
    help="Argument as KEY=VALUE. Pass once per argument.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--receipt", is_flag=True, help="Show the per-call receipt line.")
def query_cmd(
    op: str, repo: Path, args_kv: tuple[str, ...], as_json: bool, receipt: bool
) -> None:
    repo = repo.resolve()
    args = _parse_kv(args_kv)
    client = DaemonClient(repo)
    try:
        resp = client.call(op, args)
    except DaemonNotRunning as exc:
        raise click.ClickException(f"daemon not running ({exc}) — `gp daemon start`") from exc
    except DaemonError as exc:
        raise click.ClickException(f"{exc.code}: {exc}") from exc

    if as_json:
        click.echo(json.dumps(resp, indent=2))
        return

    fresh = resp.get("freshness", {})
    results = resp.get("results", [])
    more = resp.get("more_available", 0)
    click.echo(f"# trust: {fresh.get('trust', '?')} (token={fresh.get('freshness_token', '')})")
    if fresh.get("hint"):
        click.echo(f"# hint: {fresh['hint']}")
    for row in results:
        loc = f"{row.get('source_file', '')}:{row.get('line_number', 0)}"
        click.echo(
            f"{row.get('label', '?')}  [{row.get('kind', '?')}]  "
            f"({row.get('confidence', 1.0):.2f})  {loc}"
        )
        if row.get("snippet"):
            click.echo(f"    {row['snippet']}")
    if more:
        click.echo(f"# {more} more results available — increase budget_tokens to see them")
    if receipt and resp.get("receipt"):
        click.echo(format_receipt_line(resp["receipt"]))


# ---- helpers -------------------------------------------------------------


def _parse_kv(items: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw in items:
        if "=" not in raw:
            raise click.ClickException(f"--arg expects KEY=VALUE, got {raw!r}")
        k, v = raw.split("=", 1)
        # Best-effort scalar parsing: int → float → bool → string.
        if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
            out[k] = int(v)
            continue
        try:
            out[k] = float(v)
            continue
        except ValueError:
            pass
        lo = v.lower()
        if lo in ("true", "false"):
            out[k] = lo == "true"
            continue
        out[k] = v
    return out


def _route_intent(repo: Path, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run an intent op via the daemon when available, else in-process.

    Used by CLI commands that don't strictly need a running daemon. Returns
    the *handler payload* shape — ``{results, more_available, extra}`` —
    not the full server envelope.
    """
    client = DaemonClient(repo)
    if client.is_running():
        try:
            resp = client.call(op, args)
            return {
                "results": resp.get("results", []),
                "more_available": resp.get("more_available", 0),
                "extra": resp.get("extra", {}),
            }
        except DaemonError as exc:
            return {"error": {"code": exc.code, "message": str(exc)}}

    from ...daemon.handlers import HANDLERS as _HANDLERS
    from ...daemon.indexes import InMemoryGraph
    from ...runtime.store import Store, cache_path as _cache_path

    handler = _HANDLERS.get(op)
    if handler is None:
        return {"error": {"code": "UNKNOWN_OP", "message": f"unknown op: {op}"}}
    if not _cache_path(repo).exists():
        return {
            "error": {
                "code": "NO_GRAPH",
                "message": f"no graph cache — run `gp init --repo {repo}` first",
            }
        }
    store = Store(_cache_path(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    return handler(snap, args)


def _read_pid(repo: Path) -> int | None:
    p = pid_path(repo)
    try:
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


__all__ = ["daemon_cmd"]
