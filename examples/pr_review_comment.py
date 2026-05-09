#!/usr/bin/env python3
"""Generate a Markdown PR review comment from the diff.

Usage:
    examples/pr_review_comment.py [base [head]]

Pipes the output into ``gh pr comment`` for one-line CI integration::

    examples/pr_review_comment.py main HEAD | gh pr comment 123 --body-file -
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "main"
    head = sys.argv[2] if len(sys.argv) > 2 else "HEAD"
    repo = Path(".").resolve()

    from graphify_plus.daemon.indexes import InMemoryGraph
    from graphify_plus.daemon.review import format_review, make_review
    from graphify_plus.runtime.store import Store, cache_path

    if not cache_path(repo).exists():
        print("no graph cache; run `gp init` first", file=sys.stderr)
        return 1
    store = Store(cache_path(repo))
    try:
        snap = InMemoryGraph.from_store(store, repo)
    finally:
        store.close()
    review = make_review(snap, base=base, head=head)
    print(format_review(review))
    return 0


if __name__ == "__main__":
    sys.exit(main())
