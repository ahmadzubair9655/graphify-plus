"""Regenerate ``tests/fixtures/regression_corpus/baselines.json``.

Run this whenever an intentional adapter change lands. CI does not run
it — review the diff manually before committing.
"""

from __future__ import annotations

import json
from pathlib import Path

from graphify_plus.core.adapters import adapter_for

CORPUS_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "regression_corpus"
BASELINES = CORPUS_ROOT / "baselines.json"


def _baseline_for_language(lang_dir: Path) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for f in sorted(lang_dir.iterdir()):
        if not f.is_file() or f.suffix == ".md":
            continue
        adapter = adapter_for(f.name)
        if adapter is None:
            continue
        symbols, edges = adapter.parse(f, f.read_bytes())
        out[f.name] = {"symbols": len(symbols), "edges": len(edges)}
    return out


def main() -> None:
    result: dict[str, dict[str, dict[str, int]]] = {}
    for lang_dir in sorted(CORPUS_ROOT.iterdir()):
        if not lang_dir.is_dir():
            continue
        baseline = _baseline_for_language(lang_dir)
        if baseline:
            result[lang_dir.name] = baseline
    BASELINES.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"wrote {BASELINES}")


if __name__ == "__main__":
    main()
