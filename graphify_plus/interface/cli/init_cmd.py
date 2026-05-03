"""``gp init`` — walk a target repo, parse with tree-sitter, write SQLite cache
and a deterministic ``graph_symbols.jsonl`` for human inspection / CI diffing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
import orjson

from ...core.ingest import ingest
from ...runtime.store import Store, cache_path


def _write_jsonl(out: Path, symbols: list, edges: list) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    opt = orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE
    with out.open("wb") as f:
        f.write(
            orjson.dumps(
                {
                    "_meta": {
                        "kind": "header",
                        "version": 1,
                        "symbols": len(symbols),
                        "edges": len(edges),
                    }
                },
                option=opt,
            )
        )
        for s in symbols:
            f.write(orjson.dumps({"kind": "symbol", **s}, option=opt))
        for e in edges:
            f.write(orjson.dumps({"kind": "edge", **e}, option=opt))


@click.command("init")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    help="Target repository root (defaults to cwd).",
)
@click.option(
    "--print",
    "print_jsonl",
    is_flag=True,
    help="Emit graph_symbols.jsonl to stdout instead of writing files.",
)
@click.option(
    "--no-parallel",
    is_flag=True,
    help="Disable ProcessPoolExecutor (useful for tiny repos / debugging).",
)
def init_cmd(repo: Path, print_jsonl: bool, no_parallel: bool) -> None:
    """Build the symbol graph for the target repository."""
    repo = repo.resolve()
    result = ingest(repo, parallel=not no_parallel)

    if print_jsonl:
        opt = orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE
        out = sys.stdout.buffer
        out.write(
            orjson.dumps(
                {
                    "_meta": {
                        "kind": "header",
                        "version": 1,
                        "symbols": len(result.symbols),
                        "edges": len(result.edges),
                    }
                },
                option=opt,
            )
        )
        for s in result.symbols:
            out.write(orjson.dumps({"kind": "symbol", **s}, option=opt))
        for e in result.edges:
            out.write(orjson.dumps({"kind": "edge", **e}, option=opt))
        return

    db = cache_path(repo)
    store = Store(db)
    try:
        store.replace_all(result.symbols, result.edges)
        store.set_meta("repo_root", str(repo))
        store.set_meta("symbol_count", str(len(result.symbols)))
        store.set_meta("edge_count", str(len(result.edges)))
    finally:
        store.close()

    jsonl_out = repo / ".graphify_plus" / "graph_symbols.jsonl"
    _write_jsonl(jsonl_out, result.symbols, result.edges)

    click.echo(
        f"gp init: parsed {result.files_parsed} files, "
        f"{len(result.symbols)} symbols, {len(result.edges)} edges "
        f"(skipped {result.files_skipped})"
    )
    click.echo(f"  cache: {db}")
    click.echo(f"  graph: {jsonl_out}")


__all__ = ["init_cmd"]
