"""Tests for Batch D: Layers 22, 24, 25, 27."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.distribution import (
    VersionInfo,
    _newer,
    cache_path as version_cache,
    is_disabled,
    version_check,
)
from graphify_plus.daemon.local_llm import (
    LLMBackend,
    LLMRouting,
    RoutingRule,
    deterministic_seed,
    detect_backends,
    load_routing,
    route_for_path,
)
from graphify_plus.daemon.long_tail_ingestors import (
    collect_assets,
    ingest_long_tail,
    parse_compose,
    parse_dockerfile,
    parse_github_actions,
    parse_i18n_json,
    parse_k8s_manifest,
    parse_notebook,
    parse_openapi,
    parse_postman,
)
from graphify_plus.daemon.tutorial import (
    RECIPES,
    TUTORIAL_STEPS,
    install_recipes,
    render_recipes,
    render_tutorial,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd
from graphify_plus.runtime.store import Store, cache_path


# ---- Layer 22 ------------------------------------------------------------


def test_route_default(tmp_path: Path) -> None:
    routing = LLMRouting()
    assert route_for_path(routing, "any/file.py") == "cloud"


def test_route_with_rules() -> None:
    routing = LLMRouting(
        default="cloud",
        rules=[
            RoutingRule(pattern="*secret*", backend="local"),
            RoutingRule(pattern="src/auth/", backend="local"),
        ],
    )
    assert route_for_path(routing, "src/auth/foo.py") == "local"
    assert route_for_path(routing, "tools/secret-creds.py") == "local"
    assert route_for_path(routing, "ordinary.py") == "cloud"


def test_llm_free_overrides() -> None:
    routing = LLMRouting(default="cloud", llm_free=True)
    assert route_for_path(routing, "any.py") == "skip"


def test_load_routing_missing_returns_default(tmp_path: Path) -> None:
    routing = load_routing(tmp_path)
    assert routing.default == "cloud"


def test_load_routing_yaml(tmp_path: Path) -> None:
    p = tmp_path / ".graphify_plus" / "llm-routing.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(
        "default: local\nrules:\n  - pattern: 'src/'\n    backend: cloud\n"
    )
    routing = load_routing(tmp_path)
    assert routing.default == "local"
    assert routing.rules[0].pattern == "src/"


def test_detect_backends_returns_list() -> None:
    out = detect_backends()
    names = {b.name for b in out}
    assert {"ollama", "llama.cpp", "mlx"}.issubset(names)


def test_deterministic_seed_stable() -> None:
    assert deterministic_seed("hello") == deterministic_seed("hello")


def test_cli_local_llm(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["local-llm", "--repo", str(tmp_path), "--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert "backends" in body


# ---- Layer 24 ------------------------------------------------------------


def test_parse_notebook(tmp_path: Path) -> None:
    p = tmp_path / "x.ipynb"
    p.write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "code", "source": ["print('hi')"], "execution_count": 1},
                    {"cell_type": "markdown", "source": "# heading"},
                ]
            }
        )
    )
    rows = parse_notebook(p)
    assert len(rows) == 2
    assert rows[0].kind == "notebook_cell"


def test_parse_openapi(tmp_path: Path) -> None:
    p = tmp_path / "openapi.json"
    p.write_text(
        json.dumps(
            {
                "paths": {
                    "/users": {"get": {"summary": "list"}, "post": {"summary": "create"}},
                }
            }
        )
    )
    rows = parse_openapi(p)
    methods = {r.metadata["method"] for r in rows}
    assert {"GET", "POST"} == methods


def test_parse_postman(tmp_path: Path) -> None:
    p = tmp_path / "x.postman_collection.json"
    p.write_text(
        json.dumps(
            {
                "item": [
                    {
                        "name": "list users",
                        "request": {"method": "GET", "url": {"raw": "/users"}},
                    }
                ]
            }
        )
    )
    rows = parse_postman(p)
    assert rows
    assert rows[0].metadata["method"] == "GET"


def test_parse_dockerfile(tmp_path: Path) -> None:
    p = tmp_path / "Dockerfile"
    p.write_text("FROM python:3.11\nRUN pip install x\n")
    rows = parse_dockerfile(p)
    assert rows
    assert rows[0].metadata["image"] == "python:3.11"


def test_parse_compose(tmp_path: Path) -> None:
    p = tmp_path / "docker-compose.yml"
    p.write_text("services:\n  web:\n    image: nginx\n    ports:\n      - 80:80\n")
    rows = parse_compose(p)
    assert rows
    assert rows[0].metadata["image"] == "nginx"


def test_parse_k8s(tmp_path: Path) -> None:
    p = tmp_path / "x.yaml"
    p.write_text("apiVersion: v1\nkind: Service\nmetadata:\n  name: web\n")
    rows = parse_k8s_manifest(p)
    assert rows
    assert rows[0].metadata["k8s_kind"] == "Service"


def test_parse_github_actions(tmp_path: Path) -> None:
    p = tmp_path / "ci.yml"
    p.write_text("jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n")
    rows = parse_github_actions(p)
    assert rows
    assert rows[0].metadata["runs_on"] == "ubuntu-latest"


def test_parse_i18n_json_flattens(tmp_path: Path) -> None:
    p = tmp_path / "en.json"
    p.write_text(json.dumps({"hello": "Hi", "nav": {"home": "Home"}}))
    rows = parse_i18n_json(p)
    titles = {r.title for r in rows}
    assert {"hello", "nav.home"} == titles


def test_collect_assets(tmp_path: Path) -> None:
    (tmp_path / "logo.png").write_bytes(b"x")
    (tmp_path / "x.txt").write_text("not an asset")
    rows = collect_assets(tmp_path)
    assert any(r.title == "logo.png" for r in rows)
    assert not any(r.title == "x.txt" for r in rows)


def test_ingest_long_tail_end_to_end(repo: Path) -> None:
    (repo / "Dockerfile").write_text("FROM python:3.11\n")
    (repo / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n")
    store = Store(cache_path(repo))
    try:
        summary = ingest_long_tail(store, repo, kinds=["dockerfile", "compose"])
    finally:
        store.close()
    assert summary.get("dockerfile") == 1
    assert summary.get("compose") == 1


# ---- Layer 25 ------------------------------------------------------------


def test_tutorial_has_ten_steps() -> None:
    assert len(TUTORIAL_STEPS) == 10


def test_render_tutorial_includes_commands() -> None:
    out = render_tutorial()
    assert "gp daemon query" in out
    assert "Honest limitations" in out


def test_recipes_have_gpl() -> None:
    for r in RECIPES:
        assert r.gpl
        assert r.name


def test_render_recipes_runs() -> None:
    out = render_recipes()
    assert "untested-public-apis" in out


def test_install_recipes_writes_files(tmp_path: Path) -> None:
    out = install_recipes(tmp_path)
    assert out
    assert all(Path(p).exists() for p in out)


def test_cli_tutorial(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["tutorial"])
    assert result.exit_code == 0
    assert "tutorial" in result.output


def test_cli_recipes_install(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["recipes", "--install", "--repo", str(tmp_path)])
    assert result.exit_code == 0
    assert any(p.suffix == ".gpl" for p in (tmp_path / ".graphify_plus" / "queries").iterdir())


# ---- Layer 27 ------------------------------------------------------------


def test_newer_compares_versions() -> None:
    assert _newer("5.2.0", "5.1.0")
    assert not _newer("5.1.0", "5.2.0")
    assert not _newer("5.1.0", "5.1.0")


def test_version_check_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GP_NO_UPDATE_CHECK", "1")
    info = version_check(tmp_path, "5.1.0")
    assert info.error == "disabled"


def test_version_check_uses_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GP_NO_UPDATE_CHECK", raising=False)
    cache = version_cache(tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "current": "5.1.0",
                "latest": "5.9.0",
                "update_available": True,
                "checked_at": 9_999_999_999.0,
            }
        )
    )
    info = version_check(tmp_path, "5.1.0")
    assert info.update_available
    assert info.latest == "5.9.0"
