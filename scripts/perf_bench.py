"""Phase 0 baseline performance benchmark.

Runs three measurements against ``tests/fixtures/sample_repo``:

  1. cold ingest (full walk + parse)
  2. graph load from SQLite
  3. single-symbol lookup

Writes results to ``.graphify_plus_bench/baseline.json`` (or compares against it
when ``--check`` is passed). CI gate: regression > 15% fails.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "sample_repo"
BASELINE_DIR = REPO / ".graphify_plus_bench"
BASELINE_FILE = BASELINE_DIR / "baseline.json"

BUDGETS = {  # seconds, on the small fixture
    "ingest": 5.0,
    "graph_load": 0.5,
    "symbol_lookup": 0.01,
}


def _time(fn, repeat: int = 3) -> float:
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def run_benches() -> dict[str, float]:
    from graphify_plus.core.ingest import ingest
    from graphify_plus.runtime.store import Store, cache_path

    # Force a single full ingest first to seed the SQLite cache.
    res = ingest(FIXTURE, parallel=False)
    store = Store(cache_path(FIXTURE))
    store.replace_all(res.symbols, res.edges)
    store.close()

    def _ingest():
        ingest(FIXTURE, parallel=False)

    def _load():
        s = Store(cache_path(FIXTURE))
        s.all_symbols()
        s.all_edges()
        s.close()

    sample_id = res.symbols[0]["id"] if res.symbols else "x"

    def _lookup():
        s = Store(cache_path(FIXTURE))
        s.get_symbol(sample_id)
        s.close()

    return {
        "ingest": _time(_ingest, repeat=2),
        "graph_load": _time(_load, repeat=5),
        "symbol_lookup": _time(_lookup, repeat=20),
    }


def cmd_baseline() -> int:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    samples = run_benches()
    BASELINE_FILE.write_text(json.dumps(samples, indent=2, sort_keys=True))
    print(json.dumps(samples, indent=2))
    return 0


def cmd_check(max_regression: float) -> int:
    if not BASELINE_FILE.exists():
        print("no baseline; run --baseline first")
        return cmd_baseline()
    baseline = json.loads(BASELINE_FILE.read_text())
    current = run_benches()
    failed = []
    for k, v in current.items():
        ref = baseline.get(k)
        if ref is None:
            continue
        if v > ref * (1 + max_regression):
            failed.append((k, ref, v))
        if v > BUDGETS.get(k, float("inf")):
            failed.append((k, BUDGETS[k], v))
    print(json.dumps({"baseline": baseline, "current": current, "failed": failed}, indent=2))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--baseline", action="store_true")
    g.add_argument("--check", action="store_true")
    ap.add_argument("--max-regression", type=float, default=0.15)
    args = ap.parse_args()
    if args.check:
        return cmd_check(args.max_regression)
    return cmd_baseline()


if __name__ == "__main__":
    raise SystemExit(main())
