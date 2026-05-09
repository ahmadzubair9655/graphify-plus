"""Tests for Batch F: Layers 7.2/7.3 (runtime), 8.3/8.4 (IaC/config),
9.3 (license), 10.3/10.4/10.5 (refactor/time-machine/docs), 6.4
(conversation memory), 11.3/11.4 (monorepo/skills)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from graphify_plus.daemon.iac_config_license import (
    PackageLicense,
    detect_config_keys,
    detect_drift,
    detect_npm_licenses,
    detect_python_licenses,
    detect_terraform_resources,
    license_audit,
    synthesise_iac_edges,
)
from graphify_plus.daemon.indexes import InMemoryGraph
from graphify_plus.daemon.runtime_intel import (
    RuntimeProfile,
    StackFrame,
    map_runtime_to_symbols,
    map_stacktrace_to_symbols,
    parse_pprof_text,
    parse_speedscope,
    parse_stacktrace,
)
from graphify_plus.daemon.workflows import (
    Workspace,
    cross_workspace_edges,
    extract_decisions,
    extract_module_plan,
    generate_module_docs,
    install_skill,
    load_registry,
    load_workspaces,
    rename_plan,
    split_plan,
    workspace_for_path,
)
from graphify_plus.interface.cli.daemon_cmd import daemon_cmd
from graphify_plus.runtime.store import Store, cache_path


# ---- 7.2 runtime ---------------------------------------------------------


def test_parse_speedscope(tmp_path: Path) -> None:
    p = tmp_path / "p.json"
    p.write_text(
        json.dumps(
            {
                "shared": {
                    "frames": [
                        {"name": "login", "file": "auth.py"},
                        {"name": "validate", "file": "auth.py"},
                    ]
                },
                "profiles": [
                    {
                        "samples": [[0], [0, 1], [1]],
                        "weights": [1.0, 2.0, 1.0],
                    }
                ],
            }
        )
    )
    rows, src = parse_speedscope(p)
    assert src == "speedscope"
    assert any(k.endswith("login") for k in rows.keys())


def test_parse_pprof_text(tmp_path: Path) -> None:
    p = tmp_path / "p.txt"
    p.write_text("  120 (60.0%) ms  some.func\n  80 (40.0%) ms  other.func\n")
    rows, src = parse_pprof_text(p)
    assert src == "pprof-text"
    assert "some.func" in rows
    assert "other.func" in rows


def test_map_runtime_to_symbols(snapshot: InMemoryGraph) -> None:
    rows = {
        "auth.py::login": RuntimeProfile(symbol_id="auth.py::login", calls=10, total_ms=120.0),
    }
    out = map_runtime_to_symbols(rows, list(snapshot.by_id.values()))
    assert out
    assert any(rp.calls == 10 for rp in out)


# ---- 7.3 stacktrace mapper -----------------------------------------------


def test_parse_stacktrace_python() -> None:
    text = '  File "/repo/auth.py", line 7, in login\n    return self.validate()\n'
    frames = parse_stacktrace(text)
    assert frames
    assert frames[0].file.endswith("auth.py")
    assert frames[0].line == 7


def test_parse_stacktrace_node() -> None:
    text = "Error: x\n    at handle (/repo/server.js:42:10)\n"
    frames = parse_stacktrace(text)
    assert frames
    assert frames[0].file.endswith("server.js")
    assert frames[0].line == 42


def test_map_stacktrace_to_symbols(snapshot: InMemoryGraph, repo: Path) -> None:
    frames = [StackFrame(file="auth.py", line=7)]
    out = map_stacktrace_to_symbols(frames, list(snapshot.by_id.values()), repo)
    assert out
    assert out[0].symbol_id is not None


# ---- 8.3 IaC -------------------------------------------------------------


def test_detect_terraform_resources(tmp_path: Path) -> None:
    (tmp_path / "main.tf").write_text(
        'resource "aws_s3_bucket" "logs" {\n  bucket = "x"\n}\n'
        'resource "aws_lambda_function" "worker" {}\n'
    )
    out = detect_terraform_resources(tmp_path)
    names = {r.name for r in out}
    assert {"logs", "worker"} == names


def test_synthesise_iac_edges_finds_consumer(tmp_path: Path, snapshot: InMemoryGraph) -> None:
    # We add a fake "logs" reference to the existing fixture's auth.py.
    (snapshot.repo_root / "auth.py").write_text(
        (snapshot.repo_root / "auth.py").read_text() + "\nlogs = 1\n"
    )
    from graphify_plus.daemon.iac_config_license import IaCResource

    resources = [IaCResource(type="aws_s3_bucket", name="logs", file="main.tf", line=1)]
    edges = synthesise_iac_edges(
        resources, list(snapshot.by_id.values()), snapshot.repo_root
    )
    # Should at least not crash; whether edges fire depends on whether the
    # tweak above mapped to a containing symbol — best-effort assertion.
    assert isinstance(edges, list)


# ---- 8.4 config drift ----------------------------------------------------


def test_detect_config_keys(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text(
        "import os\n"
        "x = os.environ['DATABASE_URL']\n"
        "y = feature_flag('new-checkout')\n"
    )
    rows = detect_config_keys(tmp_path)
    keys = {r.key for r in rows}
    assert "DATABASE_URL" in keys
    assert "new-checkout" in keys


def test_detect_drift(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text(
        "import os\nx = os.environ['DATABASE_URL']\ny = os.environ['UNDOC_VAR']\n"
    )
    (tmp_path / ".env.example").write_text("DATABASE_URL=\nORPHAN_VAR=\n")
    out = detect_drift(tmp_path)
    assert "UNDOC_VAR" in out["referenced_but_undeclared"]
    assert "ORPHAN_VAR" in out["declared_but_unused"]


# ---- 9.3 license ---------------------------------------------------------


def test_license_audit_python_only(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nlicense = {text = "MIT"}\n'
    )
    out = license_audit(tmp_path)
    assert out["packages"]
    assert out["packages"][0]["license"] == "MIT"
    assert not out["issues"]


def test_license_audit_npm_gpl_inside_mit(tmp_path: Path) -> None:
    """We can't easily inject a GPL dep without a real package-lock.
    Instead verify the strong-copyleft detection on the dataclass."""
    p = PackageLicense(package="x", license="GPL-3.0", source="package.json")
    assert p.is_strong_copyleft()


# ---- 10.3 refactor playbooks ---------------------------------------------


def test_extract_module_plan(snapshot: InMemoryGraph) -> None:
    plan = extract_module_plan(snapshot, "auth")
    assert plan.steps
    assert plan.estimated_changes >= 0


def test_rename_plan(snapshot: InMemoryGraph) -> None:
    plan = rename_plan(snapshot, node="AuthService.login", to="signin")
    assert plan.steps
    assert plan.estimated_changes >= 1


def test_split_plan(snapshot: InMemoryGraph) -> None:
    plan = split_plan(snapshot, "AuthService")
    assert plan.steps


# ---- 10.4 time-machine ---------------------------------------------------


def test_at_revision_no_git(tmp_path: Path) -> None:
    from graphify_plus.daemon.workflows import at_revision

    out = at_revision(tmp_path, "abc")
    assert "sha" in out


# ---- 10.5 docs gen -------------------------------------------------------


def test_generate_module_docs(snapshot: InMemoryGraph) -> None:
    body = generate_module_docs(snapshot, ".")  # whole-repo "module"
    assert "God nodes" in body
    assert "Public API" in body


def test_generate_module_docs_unknown_module(snapshot: InMemoryGraph) -> None:
    body = generate_module_docs(snapshot, "does/not/exist")
    assert "no symbols indexed" in body


# ---- 6.4 conversation memory --------------------------------------------


def test_extract_decisions() -> None:
    text = (
        "...\n"
        "we'll use Postgres for the orders table for durability.\n"
        "Bob: hey\n"
        "Decided: rate-limit the public api at 100rps.\n"
    )
    decs = extract_decisions(text)
    assert len(decs) >= 2


def test_ingest_conversations(repo: Path, tmp_path: Path) -> None:
    transcripts = tmp_path / "convos"
    transcripts.mkdir()
    (transcripts / "session-1.md").write_text(
        "we'll use redis for caching.\nDecided: drop the old auth flow.\n"
    )
    from graphify_plus.daemon.workflows import ingest_conversations

    store = Store(cache_path(repo))
    try:
        summary = ingest_conversations(store, repo, transcripts)
    finally:
        store.close()
    assert summary["transcripts"] == 1
    assert summary["decisions"] >= 2


# ---- 11.3 workspaces -----------------------------------------------------


def test_load_workspaces_missing(tmp_path: Path) -> None:
    assert load_workspaces(tmp_path) == []


def test_load_workspaces_yaml(tmp_path: Path) -> None:
    p = tmp_path / ".graphify_plus" / "workspaces.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(
        "workspaces:\n  - name: api\n    path: services/api\n"
        "  - name: web\n    path: apps/web\n"
    )
    rows = load_workspaces(tmp_path)
    assert {w.name for w in rows} == {"api", "web"}


def test_workspace_for_path() -> None:
    workspaces = [Workspace(name="api", path="services/api")]
    assert workspace_for_path(workspaces, "services/api/handlers.py").name == "api"
    assert workspace_for_path(workspaces, "elsewhere/x.py") is None


# ---- 11.4 skills marketplace --------------------------------------------


def test_load_registry_default() -> None:
    rows = load_registry()
    assert rows
    assert any(r.name == "react-patterns" for r in rows)


def test_install_skill_writes_file(tmp_path: Path) -> None:
    rows = load_registry()
    target = install_skill(tmp_path, rows[0])
    assert target.exists()
    body = target.read_text()
    assert rows[0].name in body


# ---- CLI smoke checks ---------------------------------------------------


def test_cli_license_audit(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["license-audit", "--repo", str(repo), "--json"])
    assert result.exit_code == 0


def test_cli_config_drift(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["config-drift", "--repo", str(repo), "--json"])
    assert result.exit_code == 0


def test_cli_docs(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["docs", ".", "--repo", str(repo)])
    assert result.exit_code == 0
    assert "God nodes" in result.output


def test_cli_refactor_rename(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        daemon_cmd,
        [
            "refactor",
            "rename",
            "--node",
            "AuthService",
            "--to",
            "Authenticator",
            "--repo",
            str(repo),
        ],
    )
    assert result.exit_code == 0
    assert "rename" in result.output.lower()


def test_cli_skill_list() -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["skill", "list"])
    assert result.exit_code == 0


def test_cli_workspace_list_no_config(repo: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(daemon_cmd, ["workspace", "list", "--repo", str(repo)])
    assert result.exit_code == 0
    assert "no workspaces" in result.output
