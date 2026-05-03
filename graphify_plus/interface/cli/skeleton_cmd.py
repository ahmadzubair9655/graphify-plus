"""``gp skeleton`` — print canonical skeletons for a symbol or a whole file.

Two modes:

  gp skeleton --target Invoice.total
      Print the skeleton for the symbol whose qualified-name (or bare
      name) matches the argument. Multiple matches print all.

  gp skeleton --file path/to/file.py
      Print every skeleton in the file as a single document — the
      "API Surface View" of the file in O(KB) of text.
"""

from __future__ import annotations

from pathlib import Path

import click

from ...runtime.store import Store, cache_path


@click.command("skeleton")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    help="Target repository root (defaults to cwd).",
)
@click.option("--target", type=str, default=None, help="Qualified name, name, or symbol id.")
@click.option("--file", "file_path", type=str, default=None, help="Repo-relative POSIX file path.")
def skeleton_cmd(repo: Path, target: str | None, file_path: str | None) -> None:
    """Print canonical skeletons for symbols in the target repo."""
    if not target and not file_path:
        raise click.UsageError("Provide either --target or --file.")
    if target and file_path:
        raise click.UsageError("--target and --file are mutually exclusive.")

    repo = repo.resolve()
    db = cache_path(repo)
    if not db.exists():
        raise click.ClickException(
            f"No cache at {db}. Run 'graphify-plus init --repo {repo}' first."
        )

    store = Store(db)
    try:
        if file_path:
            syms = store.symbols_in_path(file_path)
            if not syms:
                raise click.ClickException(f"No symbols indexed for path: {file_path}")
            chunks: list[str] = []
            for s in syms:
                body = store.get_skeleton_for_symbol(s["id"])
                if body:
                    chunks.append(body.rstrip("\n"))
            click.echo("\n\n".join(chunks))
            return

        # --target lookup: try id first, then qualified_name / name.
        assert target is not None
        body = store.get_skeleton_for_symbol(target)
        if body:
            click.echo(body.rstrip("\n"))
            return

        matches = store.find_symbols_by_qualified_name(target)
        if not matches:
            raise click.ClickException(f"No symbol matches: {target}")
        for s in matches:
            b = store.get_skeleton_for_symbol(s["id"])
            if b:
                click.echo(b.rstrip("\n"))
                if len(matches) > 1:
                    click.echo("---")
    finally:
        store.close()


__all__ = ["skeleton_cmd"]
