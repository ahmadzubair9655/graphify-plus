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
