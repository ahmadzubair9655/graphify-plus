"""``gp serve`` — start the REST server."""

from __future__ import annotations

from pathlib import Path

import click

from ..rest_server import build_app, generate_token, read_token


@click.command("serve")
@click.option(
    "--repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
)
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=4000, type=int)
@click.option(
    "--gen-token",
    is_flag=True,
    help="Mint and print a new bearer token, then exit.",
)
@click.option(
    "--no-auth",
    is_flag=True,
    help="Disable auth — localhost-only development mode.",
)
def serve_cmd(repo: Path, host: str, port: int, gen_token: bool, no_auth: bool) -> None:
    repo = repo.resolve()
    if gen_token:
        token = generate_token(repo)
        click.echo(token)
        click.echo(f"# saved to: {repo}/.graphify_plus/.serve_token", err=True)
        return
    if not no_auth and read_token(repo) is None:
        raise click.ClickException(
            "No auth token. Run 'gp serve --gen-token' first, "
            "or pass --no-auth for localhost-only development."
        )
    try:
        import uvicorn
    except ImportError as exc:
        raise click.ClickException(
            "REST server requires: pip install graphify-plus[serve]"
        ) from exc
    app = build_app(repo, no_auth=no_auth)
    click.echo(f"graphify-plus serving on http://{host}:{port}/  (repo: {repo})", err=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


__all__ = ["serve_cmd"]
