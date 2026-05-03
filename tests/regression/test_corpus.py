"""11.5 — adapter regression corpus.

Each file under ``tests/fixtures/regression_corpus/<lang>/`` parses to a
fixed (symbols, edges) count recorded in ``baselines.json``. Any drift
fails CI. To intentionally update the baseline run::

    python scripts/regen_corpus_baselines.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphify_plus.core.adapters import adapter_for

CORPUS_ROOT = Path(__file__).resolve().parent.parent / "fixtures" / "regression_corpus"
BASELINES = json.loads((CORPUS_ROOT / "baselines.json").read_text())


def _cases():
    for lang, files in sorted(BASELINES.items()):
        for fname, expected in sorted(files.items()):
            yield pytest.param(lang, fname, expected, id=f"{lang}/{fname}")


@pytest.mark.parametrize("lang,fname,expected", list(_cases()))
def test_adapter_baseline(lang: str, fname: str, expected: dict) -> None:
    f = CORPUS_ROOT / lang / fname
    adapter = adapter_for(f.name)
    assert adapter is not None, f"no adapter matches {fname}"
    symbols, edges = adapter.parse(f, f.read_bytes())
    actual = {"symbols": len(symbols), "edges": len(edges)}
    assert actual == expected, (
        f"{lang}/{fname}: baseline drift {expected} -> {actual}. "
        "If intentional, run scripts/regen_corpus_baselines.py."
    )
