#!/usr/bin/env python3
"""Find untested functions in the repo.

Prereq: ``pytest --cov --cov-report=xml`` has produced ``coverage.xml``.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    coverage_xml = repo / "coverage.xml"
    if not coverage_xml.exists():
        print(f"no coverage.xml at {coverage_xml} — run `pytest --cov-report=xml` first")
        return 1

    from graphify_plus.daemon.coverage import ingest_report
    from graphify_plus.daemon.handlers import whats_untested
    from graphify_plus.daemon.indexes import InMemoryGraph
    from graphify_plus.runtime.store import Store, cache_path

    store = Store(cache_path(repo))
    try:
        summary = ingest_report(store, repo, coverage_xml)
        print(f"ingested {summary['symbols_attributed']} symbols of coverage")
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()

    out = whats_untested(snap, {"max_pct": 10.0, "budget_tokens": 4000})
    rows = out["results"]
    if not rows:
        print(out["extra"].get("reason", "no untested code below 10% threshold"))
        return 0
    print(f"\n{len(rows)} symbol(s) at <=10% coverage:\n")
    for r in rows[:25]:
        print(
            f"  {r.get('coverage_pct', 0):>5.1f}%  "
            f"{r.get('label', '?'):<60}  "
            f"{r.get('source_file')}:{r.get('line_number')}"
        )
    if len(rows) > 25:
        print(f"\n... and {len(rows) - 25} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
