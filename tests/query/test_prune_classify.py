"""Dead-code classifier tests (Task C)."""

from __future__ import annotations

import json

import networkx as nx
from click.testing import CliRunner

from graphify_plus.interface.cli.prune_cmd import prune_cmd
from graphify_plus.query.prune import (
    CATEGORIES,
    classify_all,
    classify_dead_candidate,
    confidence_for,
)


def _sym(**kw):
    base = {
        "id": kw.get("id", kw.get("name", "x")),
        "kind": "function",
        "name": "foo",
        "qualified_name": "m.foo",
        "path": "src/m.ts",
        "exported": False,
        "language": "typescript",
        "parent_id": None,
    }
    base.update(kw)
    return base


def test_classify_test_internal_by_path():
    assert classify_dead_candidate(_sym(path="src/foo/__tests__/bar.ts")) == "test_internal"
    assert classify_dead_candidate(_sym(path="src/foo/bar.test.ts")) == "test_internal"
    assert classify_dead_candidate(_sym(path="src/foo/bar.spec.tsx")) == "test_internal"
    assert classify_dead_candidate(_sym(path="tests/test_x.py")) == "test_internal"


def test_classify_dunder_method_python():
    assert (
        classify_dead_candidate(_sym(name="__init__", path="src/foo.py", language="python"))
        == "dunder_method"
    )
    # Non-python file with dunder name should not be tagged dunder.
    assert (
        classify_dead_candidate(_sym(name="__init__", path="src/foo.ts", language="typescript"))
        != "dunder_method"
    )


def test_classify_pytest_fixture():
    assert (
        classify_dead_candidate(_sym(name="x", path="src/conftest.py", language="python"))
        == "pytest_fixture"
    )
    assert (
        classify_dead_candidate(_sym(name="fixture_db", path="src/m.py", language="python"))
        == "pytest_fixture"
    )


def test_classify_prop_type():
    assert (
        classify_dead_candidate(
            _sym(kind="interface", name="ButtonProps", path="src/m.tsx")
        )
        == "prop_type"
    )
    assert (
        classify_dead_candidate(
            _sym(kind="type", name="CardProperties", path="src/m.tsx")
        )
        == "prop_type"
    )
    # function ending in Props is NOT a prop_type — only interface/type.
    assert (
        classify_dead_candidate(_sym(kind="function", name="ButtonProps", path="src/m.tsx"))
        != "prop_type"
    )


def test_classify_jsx_internal_for_nested_component():
    sym = _sym(
        kind="function",
        name="Child",
        qualified_name="m.Parent.Child",
        path="src/m.tsx",
        parent_id="parent-id",
    )
    assert classify_dead_candidate(sym) == "jsx_internal"


def test_classify_reducer_case():
    assert (
        classify_dead_candidate(_sym(name="onClickButton", path="src/m.ts"))
        == "reducer_case"
    )
    assert (
        classify_dead_candidate(_sym(name="handleSubmit", path="src/m.ts"))
        == "reducer_case"
    )
    assert (
        classify_dead_candidate(_sym(name="cartReducer", path="src/m.ts"))
        == "reducer_case"
    )


def test_classify_plausibly_dead_for_module_level_function():
    """v5.1: plausibly_dead is the module-level non-FP bucket. The previous
    `exported=True` requirement was unreachable because exported symbols
    are filtered out by `_is_protected` before they ever reach the
    classifier."""
    sym = _sym(
        name="genuinelyDead",
        qualified_name="m.genuinelyDead",
        path="src/m.ts",
        exported=False,
    )
    assert classify_dead_candidate(sym) == "plausibly_dead"


def test_classify_unknown_for_nested_helper_not_matching_fp_rules():
    """A nested helper (qname depth >= 2) in a non-JSX file with no FP
    pattern match falls through to `unknown`."""
    sym = _sym(
        name="helper",
        qualified_name="m.Outer.helper",
        path="src/m.ts",
        parent_id="p",
        exported=False,
    )
    assert classify_dead_candidate(sym) == "unknown"


def test_confidence_for_categories_in_expected_ranges():
    assert confidence_for("plausibly_dead") >= 0.7
    assert confidence_for("prop_type") < 0.5
    for low_cat in ("jsx_internal", "reducer_case", "test_internal", "pytest_fixture"):
        assert confidence_for(low_cat) < 0.3
    assert all(c in CATEGORIES for c in (
        "plausibly_dead",
        "jsx_internal",
        "reducer_case",
        "test_internal",
        "prop_type",
        "pytest_fixture",
        "dunder_method",
        "unknown",
    ))


def test_classify_all_sorts_by_descending_confidence():
    G: nx.MultiDiGraph = nx.MultiDiGraph()
    s1 = _sym(id="a", name="x", qualified_name="m.Parent.x", path="src/m.tsx", parent_id="p")
    s2 = _sym(id="b", name="topLevelDead", qualified_name="m.topLevelDead")
    s3 = _sym(id="c", name="onClick", qualified_name="m.onClick")
    for s in (s1, s2, s3):
        G.add_node(s["id"], **s)
    out = classify_all(G, {"a", "b", "c"})
    assert out[0]["likely_category"] == "plausibly_dead"
    assert out[0]["confidence"] >= out[-1]["confidence"]


def test_prune_cli_min_confidence_returns_genuine_dead_only(tmp_path):
    """End-to-end: a fixture with one genuinely-dead module-level function
    and one obvious false-positive (event handler) — `--min-confidence 0.7`
    must return the genuine one and only the genuine one."""
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.ts").write_text(
        "function genuinelyDead(): number { return 1; }\n"
        "function onClickButton(): void { /* false positive */ }\n"
        "export function alive(): number { return 1; }\n"
        "function liveCallee(): number { return alive(); }\n"
    )
    from graphify_plus.interface.cli.init_cmd import init_cmd

    res = runner.invoke(init_cmd, ["--repo", str(repo), "--no-parallel"])
    assert res.exit_code == 0, res.output

    res = runner.invoke(
        prune_cmd, ["--repo", str(repo), "--json", "--min-confidence", "0.7"]
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    qnames = {c["qualified_name"] for c in data["dead_classified"]}
    assert "m.genuinelyDead" in qnames
    assert "m.onClickButton" not in qnames  # filtered as reducer_case
    for c in data["dead_classified"]:
        assert c["confidence"] >= 0.7
        assert c["likely_category"] == "plausibly_dead"
        assert c["likely_category"] in CATEGORIES
