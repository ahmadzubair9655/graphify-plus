"""``gp plan`` — generate STAGING_PLAN.md for a task description.

The plan is written into the **target repo's** root, not into the
graphify-plus tree itself. Re-running on an existing plan does a 3-way
merge: only the auto-generated sections are replaced, human edits
elsewhere are preserved (use ``--force`` to overwrite from scratch).
"""

from __future__ import annotations

from pathlib import Path

import click

from ...query.sync_docs import regenerate
from ...runtime.store import cache_path


@click.command("plan")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--task", required=True, type=str)
@click.option("--force", is_flag=True, help="Overwrite STAGING_PLAN.md from scratch.")
def plan_cmd(repo: Path, task: str, force: bool) -> None:
    """Generate / refresh STAGING_PLAN.md."""
    repo = repo.resolve()
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    plan_path = regenerate(repo, task, force=force)
    click.echo(f"wrote {plan_path}")


__all__ = ["plan_cmd"]
