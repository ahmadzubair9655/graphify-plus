"""Phase 11.4 — per-file parse isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphify_plus.core.ingest import ingest


def test_skipped_jsonl_written_when_workers_complain(tmp_path: Path):
    # Two clean files plus one with content the adapter accepts (no
    # actual failure mode is trivially injectable without breaking
    # tree-sitter), so this test mainly exercises the write_skipped
    # path when no skips occur (skipped.jsonl absent or empty).
    (tmp_path / "a.py").write_text("def a(): pass\n")
    (tmp_path / "b.py").write_text("def b(): pass\n")
    res = ingest(tmp_path, parallel=False)
    assert res.files_parsed == 2
    assert res.files_skipped == 0
    skipped_jsonl = tmp_path / ".graphify_plus" / "skipped.jsonl"
    # File is only written when at least one skip occurs.
    assert not skipped_jsonl.exists() or skipped_jsonl.read_text() == ""


def test_strict_mode_raises_on_timeout(tmp_path: Path, monkeypatch):
    """Inject a fake worker outcome of 'timeout' and verify strict raises."""
    from graphify_plus.core import ingest as ingest_mod
    from graphify_plus.interface.errors import ParseTimeout

    (tmp_path / "a.py").write_text("def a(): pass\n")

    real_parse_one = ingest_mod._parse_one

    def fake(args):
        # First and only call: pretend the worker timed out.
        return [], [], {"ok": False, "reason": "timeout", "error": "alarm"}

    monkeypatch.setattr(ingest_mod, "_parse_one", fake)
    with pytest.raises(ParseTimeout):
        ingest(tmp_path, parallel=False, strict=True)
    # Ensure the real function still works after monkeypatch is undone.
    monkeypatch.setattr(ingest_mod, "_parse_one", real_parse_one)
    res = ingest(tmp_path, parallel=False)
    assert res.files_parsed == 1


def test_skipped_file_yields_stub_symbol_and_jsonl_entry(tmp_path: Path, monkeypatch):
    from graphify_plus.core import ingest as ingest_mod

    (tmp_path / "broken.py").write_text("def x(): pass\n")
    (tmp_path / "ok.py").write_text("def y(): pass\n")

    def fake(args):
        rel = args[1]
        if rel == "broken.py":
            return (
                [],
                [],
                {
                    "ok": False,
                    "reason": "parse_error",
                    "error": "boom",
                },
            )
        return real(args)

    real = ingest_mod._parse_one
    monkeypatch.setattr(ingest_mod, "_parse_one", fake)
    res = ingest(tmp_path, parallel=False)
    assert res.files_skipped == 1
    assert res.files_parsed == 1
    # Stub for broken file is present in the symbol list.
    stubs = [s for s in res.symbols if s.get("parse_status") == "failed"]
    assert any(s["path"] == "broken.py" for s in stubs)
    # JSONL written.
    out = tmp_path / ".graphify_plus" / "skipped.jsonl"
    assert out.exists()
    line = json.loads(out.read_text().splitlines()[0])
    assert line["path"] == "broken.py"
    assert line["reason"] == "parse_error"
