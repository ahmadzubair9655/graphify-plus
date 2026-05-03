"""``gp find`` — natural-language search over the symbol graph.

Default: BM25 over per-community indices, weighted by community density.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from ...core.symbol_graph import build as build_graph
from ...query.semantic import find as semantic_find
from ...runtime.store import Store, cache_path


@click.command("find")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--intent", "intent", required=True, type=str)
@click.option("--top", "top_k", default=8, type=int)
@click.option("--json", "as_json", is_flag=True)
def find_cmd(repo: Path, intent: str, top_k: int, as_json: bool) -> None:
    """Find symbols matching a natural-language intent."""
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
        matches = semantic_find(store, G, intent, top_k=top_k)
    finally:
        store.close()

    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "qualified_name": m.symbol.get("qualified_name"),
                        "kind": m.symbol.get("kind"),
                        "path": m.symbol.get("path"),
                        "score": round(m.score, 4),
                        "community": m.community,
                    }
                    for m in matches
                ],
                indent=2,
            )
        )
        return

    if not matches:
        click.echo("(no matches)")
        return
    for m in matches:
        click.echo(
            f"{m.score:7.3f}  c{m.community:<3}  {m.symbol.get('kind') or '':<10}  "
            f"{m.symbol.get('qualified_name') or ''}  ({m.symbol.get('path') or ''})"
        )


__all__ = ["find_cmd"]
