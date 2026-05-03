"""``gp vacuum`` — reclaim space in the repo's cache.

Runs ``VACUUM`` on the SQLite cache, removes ``cache.db.bak`` and any
expired ``cache.db.corrupt.*`` quarantine files older than 7 days,
and reports a before/after size summary.
"""

from __future__ import annotations

import time
from pathlib import Path

import click

from ...runtime.store import Store, cache_path

QUARANTINE_AGE_S = 7 * 24 * 3600


def _dir_bytes(d: Path) -> int:
    if not d.exists():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def _human(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    for u in units:
        if f < 1024.0:
            return f"{f:.1f} {u}"
        f /= 1024.0
    return f"{f:.1f} TB"


@click.command("vacuum")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def vacuum_cmd(repo: Path) -> None:
    """Reclaim space in <repo>/.graphify_plus/."""
    repo = repo.resolve()
    cache_dir = repo / ".graphify_plus"
    if not cache_dir.exists():
        raise click.ClickException(f"No cache at {cache_dir}")

    before = _dir_bytes(cache_dir)
    actions: list[str] = []

    db = cache_path(repo)
    if db.exists():
        store = Store(db)
        try:
            store.conn.execute("VACUUM")
            actions.append("VACUUM cache.db")
        finally:
            store.close()

    bak = db.with_suffix(".db.bak")
    if bak.exists():
        bak.unlink()
        actions.append("removed cache.db.bak")

    now = time.time()
    for q in cache_dir.glob("cache.db.corrupt.*"):
        try:
            if now - q.stat().st_mtime > QUARANTINE_AGE_S:
                q.unlink()
                actions.append(f"removed {q.name} (older than 7d)")
        except OSError:
            pass

    after = _dir_bytes(cache_dir)
    saved = before - after
    click.echo(f"Cache directory: {cache_dir}")
    click.echo(f"  before: {_human(before)}")
    click.echo(f"  after:  {_human(after)}")
    click.echo(f"  saved:  {_human(max(saved, 0))}")
    if actions:
        click.echo("Actions:")
        for a in actions:
            click.echo(f"  - {a}")
    else:
        click.echo("(nothing to do)")


__all__ = ["vacuum_cmd"]
