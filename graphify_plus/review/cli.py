"""
graphify_plus.review.cli — CLI for graph diff.

Usage:
    graphify-plus diff old.json new.json
    graphify-plus diff old.json new.json --format markdown
    graphify-plus diff old.json new.json --format json --output diff.json
    graphify-plus diff old.json new.json --fail-above HIGH
    graphify-plus diff old.json new.json \\
        --old-report old-report.json --new-report new-report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from graphify_plus.review.graph_diff import (
    diff_graphs,
    diff_meets_threshold,
    format_pr_comment,
    format_text_diff,
)

SEVERITY_LEVELS = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="graphify-plus diff",
        description="Compute structural diff between two graph snapshots",
    )
    parser.add_argument("old", help="Path to old graph.json")
    parser.add_argument("new", help="Path to new graph.json")
    parser.add_argument(
        "--format", choices=["markdown", "json", "text"], default="markdown",
    )
    parser.add_argument("--output", "-o", metavar="PATH", help="Write to file")
    parser.add_argument(
        "--old-report", metavar="PATH",
        help="Path to old report.json (for health-score delta)",
    )
    parser.add_argument(
        "--new-report", metavar="PATH",
        help="Path to new report.json (for health-score delta)",
    )
    parser.add_argument(
        "--fail-above", choices=SEVERITY_LEVELS, default=None,
        help="Exit non-zero if overall severity is above this threshold",
    )
    parser.add_argument("--max-items", type=int, default=5)
    parser.add_argument("--quiet", action="store_true")

    args = parser.parse_args(argv)
    old_path = Path(args.old)
    new_path = Path(args.new)

    if not old_path.exists():
        print(f"error: old graph not found: {old_path}", file=sys.stderr)
        return 2
    if not new_path.exists():
        print(f"error: new graph not found: {new_path}", file=sys.stderr)
        return 2

    try:
        diff = diff_graphs(
            old=old_path, new=new_path,
            old_report=Path(args.old_report) if args.old_report else None,
            new_report=Path(args.new_report) if args.new_report else None,
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "markdown":
        out = format_pr_comment(diff, max_items=args.max_items)
    elif args.format == "json":
        out = json.dumps(diff, indent=2)
    else:  # text
        out = format_text_diff(diff)

    if args.output:
        Path(args.output).write_text(out)
        if not args.quiet:
            print(f"Diff written to {args.output}")
    elif not args.quiet:
        print(out)

    if args.fail_above:
        if not diff_meets_threshold(diff, args.fail_above):
            if not args.quiet:
                print(
                    f"\nFAIL: severity {diff.get('overall_severity')} "
                    f"exceeds threshold {args.fail_above}",
                    file=sys.stderr,
                )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
