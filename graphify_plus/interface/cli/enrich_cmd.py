"""``gp enrich`` and ``gp blast-radius`` CLIs."""

from __future__ import annotations

import json
from pathlib import Path

import click
import orjson

from ...core.symbol_graph import build as build_graph
from ...query.blast_radius import trace as blast_trace
from ...query.git_silo import enrich as git_enrich
from ...query.lockfile import attach_to_graph as attach_lockfile
from ...query.telemetry import ingest_file as ingest_telemetry
from ...runtime.store import Store, cache_path


def _open(repo: Path) -> Store:
    if not cache_path(repo).exists():
        raise click.ClickException(
            f"No cache at {cache_path(repo)}. Run 'graphify-plus init --repo {repo}' first."
        )
    return Store(cache_path(repo))


def _persist(store: Store, G) -> None:
    """Persist the graph back into SQLite as a list of nodes/edges."""
    symbols = []
    for sid, attrs in G.nodes(data=True):
        a = dict(attrs or {})
        a["id"] = sid
        symbols.append(a)
    edges = []
    for u, v, data in G.edges(data=True):
        d = dict(data or {})
        d["src"] = u
        d["dst"] = v
        edges.append(d)
    # orjson fast-path: replace_all already json-encodes deterministically.
    store.replace_all(symbols, edges)


@click.group("enrich")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.pass_context
def enrich_cmd(ctx: click.Context, repo: Path) -> None:
    """Overlay non-structural information onto the symbol graph."""
    ctx.ensure_object(dict)
    ctx.obj["repo"] = repo.resolve()


@enrich_cmd.command("git")
@click.pass_context
def enrich_git(ctx: click.Context) -> None:
    repo = ctx.obj["repo"]
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        summary = git_enrich(repo, G)
        _persist(store, G)
    finally:
        store.close()
    click.echo(json.dumps(summary, indent=2))


@enrich_cmd.command("lockfile")
@click.pass_context
def enrich_lockfile(ctx: click.Context) -> None:
    repo = ctx.obj["repo"]
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        summary = attach_lockfile(repo, G)
        _persist(store, G)
    finally:
        store.close()
    click.echo(json.dumps(summary, indent=2))


@enrich_cmd.command("telemetry")
@click.argument(
    "spans_jsonl",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.pass_context
def enrich_telemetry(ctx: click.Context, spans_jsonl: Path) -> None:
    repo = ctx.obj["repo"]
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        summary = ingest_telemetry(spans_jsonl, G)
        _persist(store, G)
    finally:
        store.close()
    click.echo(json.dumps(summary, indent=2))


# ---- gp blast-radius -------------------------------------------------


@click.command("blast-radius")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.argument("package", type=str)
@click.option("--json", "as_json", is_flag=True)
def blast_cmd(repo: Path, package: str, as_json: bool) -> None:
    """Trace which in-repo symbols transitively depend on PACKAGE."""
    repo = repo.resolve()
    store = _open(repo)
    try:
        G = build_graph(store.all_symbols(), store.all_edges())
        report = blast_trace(G, package)
    finally:
        store.close()
    if as_json:
        click.echo(
            json.dumps(
                {
                    "package": report.package,
                    "suggestion": report.suggestion,
                    "hits": [
                        {
                            "symbol_id": h.symbol_id,
                            "qualified_name": h.qualified_name,
                            "path": h.path,
                            "depth": h.depth,
                        }
                        for h in report.hits
                    ],
                },
                indent=2,
            )
        )
        return
    click.echo(f"blast-radius: {report.package} → {len(report.hits)} hits")
    for h in report.hits[:50]:
        click.echo(f"  d{h.depth} {h.qualified_name} ({h.path})")
    click.echo(f"\nsuggestion: {report.suggestion}")
    # Make orjson import non-dead (also keeps file-size sane).
    _ = orjson


__all__ = ["blast_cmd", "enrich_cmd"]
