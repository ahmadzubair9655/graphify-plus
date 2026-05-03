"""``gp watch`` — run the live watcher in the foreground, emitting JSONL events."""

from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path

import click
import orjson

from ...runtime.store import cache_path
from ...runtime.watcher import FileUpdate, Watcher


@click.command("watch")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    help="Target repository root (defaults to cwd).",
)
def watch_cmd(repo: Path) -> None:
    """Watch the target repo for source-file edits and incrementally
    update the symbol graph. Emits one JSONL event per applied update.
    """
    repo = repo.resolve()
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )

    stopped = threading.Event()

    def _on_update(u: FileUpdate) -> None:
        sys.stdout.buffer.write(
            orjson.dumps(
                {
                    "event": "updated",
                    "path": u.path,
                    "added": u.added,
                    "removed": u.removed,
                    "changed": u.changed,
                    "elapsed_ms": round(u.elapsed_ms, 2),
                },
                option=orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE,
            )
        )
        sys.stdout.flush()

    w = Watcher(repo)
    w.subscribe(_on_update)
    w.start()

    def _shutdown(signum, frame):  # noqa: ARG001
        stopped.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    click.echo(f"gp watch: watching {repo} (Ctrl-C to stop)", err=True)
    try:
        stopped.wait()
    finally:
        w.stop()


__all__ = ["watch_cmd"]
