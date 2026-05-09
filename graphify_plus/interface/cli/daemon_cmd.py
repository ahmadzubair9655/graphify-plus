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
def start_cmd(repo: Path, detach: bool) -> None:
    repo = repo.resolve()
    client = DaemonClient(repo)
    if client.is_running():
        click.echo(f"daemon already running on {socket_path(repo)}", err=True)
        return
    if not detach:
        rc = run_server(repo)
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
    sys.exit(run_server(repo))


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
