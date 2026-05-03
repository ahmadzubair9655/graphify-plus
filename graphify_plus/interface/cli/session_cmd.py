"""``gp session`` — start / status / delta / prune.

Sessions track which skeleton hashes the AI has already received in the
current conversation. ``gp context`` (Phase 3) consults the session to
ship deltas instead of full re-sends.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ...runtime.session import Session, prune_expired
from ...runtime.store import Store, cache_path


def _open(repo: Path) -> Store:
    repo = repo.resolve()
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    return Store(cache_path(repo))


@click.group("session")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.pass_context
def session_cmd(ctx: click.Context, repo: Path) -> None:
    """Per-conversation session bookkeeping for Delta-Memory."""
    ctx.ensure_object(dict)
    ctx.obj["repo"] = repo


@session_cmd.command("start")
@click.option("--id", "session_id", default=None, help="Reuse a specific session id.")
@click.pass_context
def session_start(ctx: click.Context, session_id: str | None) -> None:
    """Start a new session and print its id."""
    store = _open(ctx.obj["repo"])
    try:
        s = Session.start(store, session_id=session_id)
    finally:
        store.close()
    click.echo(s.id)


@session_cmd.command("status")
@click.argument("session_id")
@click.pass_context
def session_status(ctx: click.Context, session_id: str) -> None:
    store = _open(ctx.obj["repo"])
    try:
        s = Session.start(store, session_id=session_id)
        click.echo(json.dumps(s.status(), indent=2))
    finally:
        store.close()


@session_cmd.command("delta")
@click.argument("session_id")
@click.option(
    "--hashes",
    default=None,
    help="Comma-separated skeleton hashes (or read JSON list from stdin if omitted).",
)
@click.pass_context
def session_delta(ctx: click.Context, session_id: str, hashes: str | None) -> None:
    """Compute (already_seen, new) for the supplied hashes and record the
    new ones as touched."""
    if hashes:
        wanted = [h.strip() for h in hashes.split(",") if h.strip()]
    else:
        wanted = json.loads(sys.stdin.read() or "[]")
    store = _open(ctx.obj["repo"])
    try:
        s = Session.start(store, session_id=session_id)
        seen, new = s.delta(wanted)
        s.touch_many(new)
        click.echo(
            json.dumps(
                {"already_seen": sorted(seen), "new": sorted(new)},
                indent=2,
            )
        )
    finally:
        store.close()


@session_cmd.command("prune")
@click.pass_context
def session_prune(ctx: click.Context) -> None:
    """Delete session rows older than 24 h."""
    store = _open(ctx.obj["repo"])
    try:
        n = prune_expired(store)
    finally:
        store.close()
    click.echo(f"pruned {n} expired session rows")


__all__ = ["session_cmd"]
