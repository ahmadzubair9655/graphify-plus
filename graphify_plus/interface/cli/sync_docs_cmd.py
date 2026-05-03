"""``gp sync-docs`` — run the sync-docs daemon in foreground.

Watches the target repo. As files in the dependency order are touched,
the matching rows in ``STAGING_PLAN.md`` are annotated ``✓``.
"""

from __future__ import annotations

import signal
import threading
from pathlib import Path

import click

from ...query.sync_docs import SyncDocsDaemon
from ...runtime.store import cache_path
from ...runtime.watcher import FileUpdate


@click.command("sync-docs")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
def sync_docs_cmd(repo: Path) -> None:
    """Run the sync-docs daemon in foreground."""
    repo = repo.resolve()
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    plan = repo / "STAGING_PLAN.md"
    if not plan.exists():
        raise click.ClickException(
            f"No STAGING_PLAN.md at {plan}. Run 'graphify-plus plan --task ...' first."
        )

    stopped = threading.Event()
    daemon = SyncDocsDaemon(repo)

    def _on_event(u: FileUpdate) -> None:
        click.echo(f"sync-docs: {u.path} updated", err=True)

    daemon.start(_on_event)

    def _shutdown(signum, frame):  # noqa: ARG001
        stopped.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    click.echo(f"gp sync-docs: watching {repo} (Ctrl-C to stop)", err=True)
    try:
        stopped.wait()
    finally:
        daemon.stop()


__all__ = ["sync_docs_cmd"]
