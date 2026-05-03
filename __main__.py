"""
graphify-plus CLI dispatcher.

Subcommands:
  enhance   — run the v2 enhancement pipeline on graphify-out/graph.json
  audit     — run the adversarial audit
  diff      — compute graph diff between two snapshots
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="graphify-plus")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("enhance", add_help=False, help="Run enhancement pipeline")
    sub.add_parser("audit", add_help=False, help="Adversarial audit")
    sub.add_parser("diff", add_help=False, help="Graph diff between two snapshots")

    args, rest = parser.parse_known_args()

    if args.cmd == "enhance":
        from graphify_plus.pipeline import enhance_existing_graph
        from pathlib import Path
        import json
        # Simple argparse for backward-compat with existing CLI
        ep = argparse.ArgumentParser(prog="graphify-plus enhance")
        ep.add_argument("graph", nargs="?", default="graphify-out/graph.json")
        ep.add_argument("--corpus", default=None)
        ep.add_argument("--quiet", action="store_true")
        ea = ep.parse_args(rest)
        result = enhance_existing_graph(
            graph_json_path=Path(ea.graph),
            corpus_root=Path(ea.corpus) if ea.corpus else None,
            verbose=not ea.quiet,
        )
        if not ea.quiet:
            print(f"\nDone. report.json -> {result['report_path']}")
        return 0

    if args.cmd == "audit":
        from graphify_plus.audit.cli import main as audit_main
        return audit_main(rest)

    if args.cmd == "diff":
        from graphify_plus.review.cli import main as diff_main
        return diff_main(rest)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
