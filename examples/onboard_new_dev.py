#!/usr/bin/env python3
"""Generate a guided-tour walkthrough for a new contributor.

    examples/onboard_new_dev.py [persona]

Writes Markdown to stdout. Pipe to a wiki page or paste into
CONTRIBUTING.md.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    persona = sys.argv[1] if len(sys.argv) > 1 else "engineer"
    repo = Path(".").resolve()

    from graphify_plus.daemon.indexes import InMemoryGraph
    from graphify_plus.daemon.onboarding import format_plan, make_plan
    from graphify_plus.runtime.store import Store, cache_path

    if not cache_path(repo).exists():
        print("no graph cache; run `gp init` first", file=sys.stderr)
        return 1
    store = Store(cache_path(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    plan = make_plan(snap, persona=persona)
    print(format_plan(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
