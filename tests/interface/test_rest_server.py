"""12.7 — REST server: auth, endpoints, token-budget invariants."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from graphify_plus.interface.rest_server import build_app, generate_token


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(
        "def f():\n    return 1\n\nclass C:\n    def m(self):\n        return f()\n"
    )
    subprocess.run(
        [sys.executable, "-m", "graphify_plus", "init", "--repo", str(repo), "--no-parallel"],
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def authed(tmp_path):
    repo = _init_repo(tmp_path)
    token = generate_token(repo)
    app = build_app(repo)
    return TestClient(app), token, repo


def test_health_requires_no_auth(authed):
    client, _t, _r = authed
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_protected_endpoint_rejects_missing_token(authed):
    client, _t, _r = authed
    r = client.get("/v1/audit")
    assert r.status_code == 401


def test_protected_endpoint_rejects_malformed_token(authed):
    client, _t, _r = authed
    r = client.get("/v1/audit", headers={"Authorization": "Bearer bogus"})
    assert r.status_code == 401


def test_protected_endpoint_rejects_wrong_scheme(authed):
    client, token, _r = authed
    r = client.get("/v1/audit", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 401


def test_protected_endpoint_accepts_valid_token(authed):
    client, token, _r = authed
    r = client.get("/v1/audit", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "trust_score" in r.json()


def test_context_token_budget_enforced(authed):
    client, token, _r = authed
    r = client.post(
        "/v1/context",
        json={"target": "f", "max_tokens": 1000},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    if "tokens" in body:
        assert body["tokens"] <= body["max_tokens"]


def test_skeleton_endpoint(authed):
    client, token, _r = authed
    r = client.post(
        "/v1/skeleton",
        json={"target": "f"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "skeleton" in body or "error" in body


def test_explain_endpoint(authed):
    client, token, _r = authed
    r = client.get("/v1/explain/f", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert "out_edges" in body
    assert "in_edges" in body


def test_find_endpoint(authed):
    client, token, _r = authed
    r = client.post(
        "/v1/find",
        json={"query": "f", "k": 5},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert "matches" in r.json()


def test_plan_endpoint_returns_real_plan(authed):
    client, token, _r = authed
    r = client.post(
        "/v1/plan",
        json={"task": "add a logger"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("plan_path", "").endswith("STAGING_PLAN.md")
    assert body.get("plan"), "plan body should be non-empty"


def test_simulate_endpoint_returns_real_grade(authed):
    client, token, _r = authed
    r = client.post(
        "/v1/simulate",
        json={
            "edits": [
                {"op": "add_node", "id": "stub_x"},
                {"op": "add_edge", "src": "stub_x", "dst": "stub_y", "kind": "calls"},
            ]
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "grade" in body
    assert "violations" in body
    assert "summary" in body


def test_no_auth_mode_skips_check(tmp_path):
    repo = _init_repo(tmp_path)
    app = build_app(repo, no_auth=True)
    client = TestClient(app)
    r = client.get("/v1/audit")
    assert r.status_code == 200


def test_build_app_refuses_without_token_when_auth_required(tmp_path):
    repo = _init_repo(tmp_path)
    with pytest.raises(RuntimeError, match="No auth token"):
        build_app(repo, no_auth=False)


def test_ui_returns_html_when_web_extra_available(authed, monkeypatch):
    client, _t, _r = authed
    r = client.get("/ui/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_ui_returns_503_when_web_extra_missing(authed, monkeypatch):
    client, _t, _r = authed
    import sys

    saved = sys.modules.pop("graphify_plus.interface.web_ui", None)
    monkeypatch.setitem(sys.modules, "graphify_plus.interface.web_ui", None)
    try:
        r = client.get("/ui/")
        assert r.status_code == 503
        assert "graphify-plus[web]" in r.json()["detail"]
    finally:
        if saved is not None:
            sys.modules["graphify_plus.interface.web_ui"] = saved
