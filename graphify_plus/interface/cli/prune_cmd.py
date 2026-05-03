"""``gp prune`` — list dead-code, orphan, and (optionally) deprecated
symbol ids that the budgeter should exclude from any framed context.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from ...core.symbol_graph import build as build_graph
from ...query.prune import report as prune_report
from ...runtime.store import Store, cache_path


@click.command("prune")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option(
    "--aggressive",
    is_flag=True,
    help="Also include 'deprecated' symbols (docstring or path-based).",
)
@click.option("--json", "as_json", is_flag=True)
def prune_cmd(repo: Path, aggressive: bool, as_json: bool) -> None:
    """Print symbol ids the budgeter should exclude."""
    repo = repo.resolve()
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    store = Store(cache_path(repo))
    try:
        symbols = store.all_symbols()
        edges = store.all_edges()
        G = build_graph(symbols, edges)
        rep = prune_report(G)
    finally:
        store.close()

    if as_json:
        click.echo(
            json.dumps(
                {
                    "dead": sorted(rep.dead),
                    "orphans": sorted(rep.orphans),
                    "deprecated": sorted(rep.deprecated),
                    "exclude": sorted(rep.all_ids(aggressive=aggressive)),
                },
                indent=2,
            )
        )
        return

    click.echo(f"dead       {len(rep.dead)}")
    click.echo(f"orphans    {len(rep.orphans)}")
    click.echo(f"deprecated {len(rep.deprecated)}")
    excl = rep.all_ids(aggressive=aggressive)
    click.echo(f"exclude    {len(excl)} (aggressive={aggressive})")
    for sid in sorted(excl):
        click.echo(f"  {sid}")


__all__ = ["prune_cmd"]
