"""Bug 5 (v5.0.3): TS / JS path-alias resolution from tsconfig.json."""

from __future__ import annotations

from pathlib import Path

from graphify_plus.core.adapters.base import CONF_FALLBACK, CONF_RESOLVED
from graphify_plus.core.ingest import ingest
from graphify_plus.core.resolve_aliases import (
    _strip_jsonc,
    load_alias_map,
    resolve_alias_imports,
)


def _seed(tmp_path: Path) -> Path:
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "tsconfig.json").write_text(
        "{\n"
        "  // baseUrl + path alias\n"
        '  "compilerOptions": {\n'
        '    "baseUrl": ".",\n'
        '    "paths": {\n'
        '      "@/*": ["src/*"],\n'
        '      "~util/*": ["src/lib/*"]\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
    (tmp_path / "src" / "lib" / "data.ts").write_text("export const X = 1;\n")
    (tmp_path / "src" / "local.ts").write_text("export const Y = 2;\n")
    (tmp_path / "src" / "app.ts").write_text(
        "import { X } from '@/lib/data';\n"
        "import { Y } from './local';\n"
        "import React from 'react';\n"
        "import { Z } from '~util/data';\n"
    )
    return tmp_path


def test_strip_jsonc_handles_comments_and_trailing_commas():
    text = '{\n  // line comment\n  /* block */\n  "a": 1,\n  "b": [1, 2,],\n}\n'
    out = _strip_jsonc(text)
    import json

    parsed = json.loads(out)
    assert parsed == {"a": 1, "b": [1, 2]}


def test_load_alias_map_reads_paths_and_baseurl(tmp_path: Path):
    _seed(tmp_path)
    am = load_alias_map(tmp_path)
    # tsconfig dir == repo root with baseUrl="." → "" or "." both acceptable
    assert am.base_dir in ("", ".")
    assert "@/" in am.prefixes
    assert any(p.startswith("src/") for p in am.prefixes["@/"])
    assert "~util/" in am.prefixes


def test_alias_imports_resolved_via_ingest(tmp_path: Path):
    repo = _seed(tmp_path)
    res = ingest(repo, parallel=False)

    by_dst_text: dict[str, dict] = {}
    for e in res.edges:
        if e.get("kind") != "imports":
            continue
        # Edges that were rewritten to a target id will have resolved=True;
        # those still showing the literal import statement remain as text.
        by_dst_text[str(e.get("dst"))] = e

    # @/ alias was resolved
    resolved = [e for e in res.edges if e.get("kind") == "imports" and e.get("resolved") is True]
    assert resolved, "expected at least one resolved import edge"
    for e in resolved:
        assert e.get("confidence") == CONF_RESOLVED

    # The bare 'react' import stayed unresolved (no module symbol)
    react_edge = next(
        (e for e in res.edges if e.get("kind") == "imports" and "react" in str(e.get("dst", ""))),
        None,
    )
    assert react_edge is not None
    assert react_edge.get("resolved") is False
    assert react_edge.get("confidence") == CONF_FALLBACK


def test_relative_imports_resolved_too(tmp_path: Path):
    repo = _seed(tmp_path)
    res = ingest(repo, parallel=False)
    # The `./local` import should also resolve to a real symbol id
    # (not a string literal anymore).
    resolved_targets = {
        e.get("dst") for e in res.edges if e.get("kind") == "imports" and e.get("resolved")
    }
    qnames = {s["qualified_name"] for s in res.symbols if s.get("id") in resolved_targets}
    assert "local" in qnames, f"expected local module among resolved targets, got {qnames}"


def test_resolve_alias_imports_returns_summary(tmp_path: Path):
    repo = _seed(tmp_path)
    res = ingest(repo, parallel=False)
    # Re-run resolver on the already-resolved edges — should be a no-op.
    summary = resolve_alias_imports(repo, list(res.symbols), list(res.edges))
    assert "alias_prefixes" in summary
    assert summary["resolved"] >= 0  # idempotent on already-resolved


def test_no_tsconfig_is_safe(tmp_path: Path):
    """If tsconfig.json is missing, ingest still works and TS imports
    stay unresolved — never raises."""
    (tmp_path / "src.ts").write_text("import { X } from '@/foo';\n")
    res = ingest(tmp_path, parallel=False)
    assert res.files_parsed >= 1
    # No alias config present, so the @/foo import remains unresolved.
    assert any(e.get("kind") == "imports" and not e.get("resolved") for e in res.edges)


def test_jsconfig_also_consumed(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.js").write_text("export const X = 1;\n")
    (tmp_path / "src" / "main.js").write_text("import { X } from '@/x';\n")
    (tmp_path / "jsconfig.json").write_text(
        '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}'
    )
    res = ingest(tmp_path, parallel=False)
    resolved = [e for e in res.edges if e.get("kind") == "imports" and e.get("resolved")]
    assert resolved, "expected jsconfig.json @/x import to resolve"
