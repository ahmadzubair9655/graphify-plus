"""Phase 6 end-to-end: shadow + guardrails + drift, all on the unified
rules engine.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from graphify_plus.core import cas
from graphify_plus.core.ingest import ingest
from graphify_plus.core.skeletonizer import skeletonize_all
from graphify_plus.core.symbol_graph import build as build_graph
from graphify_plus.query.drift import check as drift_check
from graphify_plus.query.guardrails import evaluate_payload
from graphify_plus.query.shadow import simulate
from graphify_plus.runtime.overlay import empty
from graphify_plus.runtime.rules import RuleSet
from graphify_plus.runtime.store import Store, cache_path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


def _seed(tmp_path: Path, rules_yaml: str | None = None) -> Path:
    target = tmp_path / "repo"
    shutil.copytree(FIXTURE, target)
    res = ingest(target, parallel=False)
    skel = skeletonize_all(res.symbols)
    s = Store(cache_path(target))
    try:
        s.replace_all(res.symbols, res.edges)
        for sid, body in skel.items():
            h = cas.put(s, body)
            s.link_skeleton(sid, h)
    finally:
        s.close()
    if rules_yaml is not None:
        (target / ".graphify_plus" / "rules.yaml").write_text(rules_yaml)
    return target


_RULES = """
layers:
  ui: ["frontend/**"]
  api: ["backend/api*"]
  database: ["backend/billing/**"]
rules:
  - id: no-ui-to-db
    forbid_edge:
      from_layer: ui
      to_layer: database
      kinds: [imports, calls]
    severity: error
"""


def test_shadow_grades_proposed_change(tmp_path: Path):
    repo = _seed(tmp_path, _RULES)
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        ui = next(x for x in s.all_symbols() if x["qualified_name"].startswith("CheckoutButton."))
        db = next(x for x in s.all_symbols() if x["qualified_name"] == "invoice.Invoice.total")
    finally:
        s.close()
    rs = RuleSet(
        layers={
            "ui": ["frontend/**"],
            "api": ["backend/api*"],
            "database": ["backend/billing/**"],
        },
        rules=[
            {
                "id": "no-ui-to-db",
                "severity": "error",
                "forbid_edge": {
                    "from_layer": "ui",
                    "to_layer": "database",
                    "kinds": ["imports", "calls"],
                },
            }
        ],
    )
    overlay = empty(G)
    overlay.add_edge(ui["id"], db["id"], kind="imports")
    result = simulate(overlay, rs)
    assert result.grade != "A"
    assert any(v.rule_id == "no-ui-to-db" for v in result.violations)


def test_drift_check_finds_no_violation_on_clean_fixture(tmp_path: Path):
    repo = _seed(tmp_path, _RULES)
    rs = RuleSet(
        layers={
            "ui": ["frontend/**"],
            "api": ["backend/api*"],
            "database": ["backend/billing/**"],
        },
        rules=[
            {
                "id": "no-ui-to-db",
                "severity": "error",
                "forbid_edge": {
                    "from_layer": "ui",
                    "to_layer": "database",
                    "kinds": ["imports", "calls"],
                },
            }
        ],
    )
    report = drift_check(repo, rs)
    assert report.grade == "A"
    assert report.violations == []


def test_guardrails_blocks_ui_to_db_call(tmp_path: Path):
    repo = _seed(tmp_path, _RULES)
    s = Store(cache_path(repo))
    try:
        G = build_graph(s.all_symbols(), s.all_edges())
        rs = RuleSet(
            layers={
                "ui": ["frontend/**"],
                "api": ["backend/api*"],
                "database": ["backend/billing/**"],
            },
            rules=[
                {
                    "id": "no-ui-to-db",
                    "severity": "error",
                    "forbid_edge": {
                        "from_layer": "ui",
                        "to_layer": "database",
                        "kinds": ["calls", "imports"],
                    },
                }
            ],
        )
        decision = evaluate_payload(
            s,
            G,
            {
                "file": "frontend/src/CheckoutButton.tsx",
                "add_calls": [["CheckoutButton.CheckoutButton", "invoice.Invoice.total"]],
            },
            rs,
        )
    finally:
        s.close()
    assert decision.block
    assert decision.rule_id == "no-ui-to-db"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "graphify_plus", *args],
        capture_output=True,
        check=False,
    )


def test_drift_writes_into_target_repo(tmp_path: Path):
    repo = _seed(tmp_path, _RULES)
    out = _run("drift", "--repo", str(repo), "check")
    assert out.returncode == 0, out.stderr.decode()
    drift_md = repo / ".graphify_plus" / "drift_report.md"
    assert drift_md.exists()
    text = drift_md.read_text()
    assert "Drift Report" in text


def test_guardrails_cli_returns_block_payload(tmp_path: Path):
    repo = _seed(tmp_path, _RULES)
    payload = json.dumps(
        {
            "file": "frontend/src/CheckoutButton.tsx",
            "add_calls": [["CheckoutButton.CheckoutButton", "invoice.Invoice.total"]],
        }
    )
    out = subprocess.run(
        [sys.executable, "-m", "graphify_plus", "guardrails", "--repo", str(repo)],
        input=payload.encode(),
        capture_output=True,
        check=False,
    )
    assert out.returncode == 1, out.stderr.decode()
    parsed = json.loads(out.stdout.decode())
    assert parsed["block"] is True
    assert parsed["rule_id"] == "no-ui-to-db"
