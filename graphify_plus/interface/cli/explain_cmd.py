"""``gp explain <symbol>`` — render a one-screen brief on a single symbol.

Shows the skeleton, 1-hop neighbours (with confidence), and counts of
inbound/outbound edges. Read-only.
"""

from __future__ import annotations

from pathlib import Path

import click

from ...core.symbol_graph import build as build_graph
from ...runtime.store import Store, cache_path


@click.command("explain")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.argument("symbol", type=str)
def explain_cmd(repo: Path, symbol: str) -> None:
    """Print a brief on ``symbol`` (qualified name or id)."""
    repo = repo.resolve()
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    store = Store(cache_path(repo))
    try:
        if store.get_symbol(symbol) is not None:
            sid = symbol
        else:
            matches = store.find_symbols_by_qualified_name(symbol)
            if not matches:
                raise click.ClickException(f"No symbol matches: {symbol}")
            if len(matches) > 1:
                names = ", ".join(s["qualified_name"] for s in matches[:5])
                raise click.ClickException(f"Ambiguous {symbol!r}: {names}")
            sid = matches[0]["id"]
        sym = store.get_symbol(sid)
        if sym is None:
            raise click.ClickException(f"Symbol vanished: {sid}")
        symbols = store.all_symbols()
        edges = store.all_edges()
        G = build_graph(symbols, edges)
        skeleton = store.get_skeleton_for_symbol(sid)

        click.echo(f"# {sym.get('qualified_name')} ({sym.get('kind')})")
        click.echo(f"path: {sym.get('path')}  span: {sym.get('span')}")
        click.echo(f"language: {sym.get('language')}")
        if sym.get("docstring"):
            click.echo(f"\n{sym['docstring']}")
        if skeleton:
            click.echo("\n## Skeleton\n")
            click.echo(skeleton)

        click.echo("\n## Outbound edges")
        any_out = False
        for u, v, _k, data in G.out_edges(sid, keys=True, data=True):
            del u
            conf = data.get("confidence", "?")
            label = G.nodes[v].get("qualified_name") or v
            mark = "  [low]" if isinstance(conf, (int, float)) and conf < 0.7 else ""
            click.echo(f"  -[{data.get('kind')}]-> {label}  conf={conf}{mark}")
            any_out = True
        if not any_out:
            click.echo("  (none)")

        click.echo("\n## Inbound edges")
        any_in = False
        for u, v, _k, data in G.in_edges(sid, keys=True, data=True):
            del v
            conf = data.get("confidence", "?")
            label = G.nodes[u].get("qualified_name") or u
            mark = "  [low]" if isinstance(conf, (int, float)) and conf < 0.7 else ""
            click.echo(f"  {label} -[{data.get('kind')}]->  conf={conf}{mark}")
            any_in = True
        if not any_in:
            click.echo("  (none)")
    finally:
        store.close()


__all__ = ["explain_cmd"]
