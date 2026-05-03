"""``gp context`` — produce a token-budgeted context block for an
``(entry, target)`` pair.

Usage::

    gp context --entry frontend/CheckoutButton.tsx --target Invoice.total
    gp context --target make_invoice --max-tokens 2000

If ``--entry`` is omitted, the entry is the first symbol of the target's
file (a sensible default for "give me everything I need to read this
function").
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import networkx as nx

from ...core.symbol_graph import build as build_graph
from ...query.budget import frame_to_budget
from ...query.partition import compute_partition
from ...query.prune import report as prune_report
from ...runtime.store import Store, cache_path


def _resolve_entry(store: Store, entry_path: str | None, target_id: str) -> str:
    """Pick a sensible entry symbol id."""
    if entry_path:
        syms = store.symbols_in_path(entry_path)
        if not syms:
            raise click.ClickException(f"No symbols at entry path: {entry_path}")
        # Prefer the module symbol if present, else first.
        for s in syms:
            if s.get("kind") == "module":
                return s["id"]
        return syms[0]["id"]
    # Default: target's own module.
    target = store.get_symbol(target_id)
    if target is None:
        raise click.ClickException(f"Target symbol not in cache: {target_id}")
    same_path = store.symbols_in_path(target.get("path") or "")
    for s in same_path:
        if s.get("kind") == "module":
            return s["id"]
    return target_id


def _resolve_target(store: Store, target: str) -> str:
    if store.get_symbol(target) is not None:
        return target
    matches = store.find_symbols_by_qualified_name(target)
    if not matches:
        raise click.ClickException(f"No symbol matches: {target}")
    if len(matches) > 1:
        names = ", ".join(s["qualified_name"] for s in matches)
        raise click.ClickException(
            f"Ambiguous target {target!r}: matches {names}. Use a fully qualified name."
        )
    return matches[0]["id"]


@click.command("context")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--entry", type=str, default=None, help="Repo-relative path of the entry file.")
@click.option("--target", type=str, required=True, help="Symbol name, qualified name, or id.")
@click.option("--max-tokens", "max_tokens", type=int, default=4000)
@click.option("--json-manifest", is_flag=True, help="Append a JSON manifest after the context.")
@click.option(
    "--apply-prune",
    is_flag=True,
    help="Exclude dead-code + orphan symbols from the framed context.",
)
@click.option(
    "--aggressive-prune",
    is_flag=True,
    help="Implies --apply-prune; also excludes deprecated symbols.",
)
def context_cmd(
    repo: Path,
    entry: str | None,
    target: str,
    max_tokens: int,
    json_manifest: bool,
    apply_prune: bool,
    aggressive_prune: bool,
) -> None:
    """Print a token-budgeted slice of the symbol graph."""
    repo = repo.resolve()
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    store = Store(cache_path(repo))
    try:
        target_id = _resolve_target(store, target)
        entry_id = _resolve_entry(store, entry, target_id)
        symbols = store.all_symbols()
        edges = store.all_edges()
        G = build_graph(symbols, edges)
        try:
            partition = compute_partition(G, entry_id, target_id)
        except nx.NetworkXNoPath:
            raise click.ClickException(
                f"No path from {entry_id} to {target_id}. Try a different entry."
            ) from None
        excl: set[str] = set()
        if apply_prune or aggressive_prune:
            excl = prune_report(G).all_ids(aggressive=aggressive_prune)
        framed = frame_to_budget(partition, store, max_tokens=max_tokens, exclude=excl)
        click.echo(framed.text.rstrip("\n"))
        if json_manifest:
            click.echo(
                "\n--- manifest ---\n"
                + json.dumps(
                    {
                        "tokens": framed.tokens,
                        "max_tokens": max_tokens,
                        "manifest_hashes": framed.manifest,
                        "truncated_count": framed.truncated_count,
                        "cut_count": framed.cut_count,
                    },
                    indent=2,
                )
            )
    finally:
        store.close()


__all__ = ["context_cmd"]
