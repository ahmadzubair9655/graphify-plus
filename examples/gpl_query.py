#!/usr/bin/env python3
"""Run a custom GPL (Graphify-Plus Query Language) query.

Examples:

    examples/gpl_query.py "FIND nodes WHERE test_coverage < 0.1"
    examples/gpl_query.py "MATCH (n:Function)-[:calls*1..3]->(m) RETURN label, source_file, line_number ORDER BY degree DESC LIMIT 20"
    examples/gpl_query.py --nl "untested public api functions"
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if "--nl" in sys.argv:
        idx = sys.argv.index("--nl")
        nl = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        gpl = ""
    else:
        nl = ""
        gpl = " ".join(sys.argv[1:])
    if not gpl and not nl:
        print(__doc__)
        return 1

    from graphify_plus.daemon.handlers import gpl_query
    from graphify_plus.daemon.indexes import InMemoryGraph
    from graphify_plus.runtime.store import Store, cache_path

    repo = Path(".").resolve()
    if not cache_path(repo).exists():
        print("no graph cache; run `gp init` first", file=sys.stderr)
        return 1
    store = Store(cache_path(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    out = gpl_query(snap, {"query": gpl, "nl": nl})
    if "error" in out:
        print(f"query error: {out['error']['message']}", file=sys.stderr)
        return 1
    extra = out.get("extra", {})
    if "nl_to_gpl" in extra:
        print(f"# translated: {extra['nl_to_gpl']}")
    print(f"# {extra.get('count', 0)} match(es)")
    for row in out["results"]:
        print("  " + "  ".join(f"{k}={v!r}" for k, v in row.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
