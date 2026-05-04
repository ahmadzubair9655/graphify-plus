"""``gp prune`` — list dead-code, orphan, and (optionally) deprecated
symbol ids that the budgeter should exclude from any framed context.

Each candidate carries a ``likely_category`` and ``confidence`` so callers
can filter false-positive-prone categories (JSX-internal helpers, reducer
cases, test fixtures, prop types, dunder methods) and focus on
high-confidence dead code.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from ...core.symbol_graph import build as build_graph
from ...query.prune import classify_all
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
@click.option(
    "--min-confidence",
    type=click.FloatRange(0.0, 1.0),
    default=0.0,
    show_default=True,
    help="Filter dead candidates to those with classifier confidence >= this value.",
)
def prune_cmd(repo: Path, aggressive: bool, as_json: bool, min_confidence: float) -> None:
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

    classified = classify_all(G, rep.dead)
    filtered = [c for c in classified if c["confidence"] >= min_confidence]
    filtered_ids = {c["id"] for c in filtered}

    if as_json:
        click.echo(
            json.dumps(
                {
                    "dead": sorted(filtered_ids),
                    "dead_classified": filtered,
                    "orphans": sorted(rep.orphans),
                    "deprecated": sorted(rep.deprecated),
                    "exclude": sorted(
                        filtered_ids
                        | set(rep.orphans)
                        | (set(rep.deprecated) if aggressive else set())
                    ),
                    "min_confidence": min_confidence,
                },
                indent=2,
            )
        )
        return

    click.echo(f"dead       {len(filtered)} (>= confidence {min_confidence})")
    click.echo(f"orphans    {len(rep.orphans)}")
    click.echo(f"deprecated {len(rep.deprecated)}")
    excl = filtered_ids | set(rep.orphans) | (set(rep.deprecated) if aggressive else set())
    click.echo(f"exclude    {len(excl)} (aggressive={aggressive})")
    for cand in filtered:
        click.echo(
            f"  [{cand['likely_category']:<14} {cand['confidence']:.2f}] "
            f"{cand['qualified_name']}  ({cand['path']})"
        )
    for sid in sorted(rep.orphans):
        click.echo(f"  [orphan         1.00] {sid}")


__all__ = ["prune_cmd"]
