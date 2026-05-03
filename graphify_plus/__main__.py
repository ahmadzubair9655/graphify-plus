"""
graphify-plus CLI dispatcher.

Subcommands:
  audit     — run the adversarial audit on a graphify graph.json
  diff      — compute graph diff between two snapshots
  enhance   — (not yet shipped — see README roadmap)

Examples:
  graphify-plus audit path/to/graph.json
  graphify-plus audit path/to/graph.json --json
  graphify-plus diff old/graph.json new/graph.json --format markdown
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import click


def _load_graph(path: Path):
    """Load a graphify graph.json into a networkx.Graph."""
    import networkx as nx
    with open(path) as f:
        data = json.load(f)
    G = nx.Graph()
    for n in data.get("nodes", []):
        node_id = n.get("id")
        attrs = {k: v for k, v in n.items() if k != "id"}
        G.add_node(node_id, **attrs)
    for e in data.get("edges", data.get("links", [])):
        src = e.get("source")
        dst = e.get("target")
        attrs = {k: v for k, v in e.items() if k not in ("source", "target")}
        G.add_edge(src, dst, **attrs)
    return G


def _run_audit(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="graphify-plus audit")
    parser.add_argument("graph", help="Path to graphify graph.json")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text")
    parser.add_argument("--output", "-o", metavar="PATH", help="Write result to file instead of stdout")
    parser.add_argument(
        "--fail-below",
        choices=["A", "B", "C", "D", "F"],
        default=None,
        help="Exit non-zero if overall grade is below this letter",
    )
    args = parser.parse_args(rest)

    graph_path = Path(args.graph)
    if not graph_path.exists():
        print(f"error: graph file not found: {graph_path}", file=sys.stderr)
        return 2

    from graphify_plus.audit.probe import (
        run_audit,
        format_audit_report,
        audit_meets_threshold,
    )

    G = _load_graph(graph_path)
    result = run_audit(G)

    if args.json:
        rendered = json.dumps(result, indent=2, default=str)
    else:
        rendered = format_audit_report(result)

    if args.output:
        Path(args.output).write_text(rendered)
        print(f"audit -> {args.output}")
    else:
        print(rendered)

    if args.fail_below and not audit_meets_threshold(result, args.fail_below):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]

    if cmd == "audit":
        from graphify_plus.audit.cli import main as audit_main
        return audit_main(rest)

    if cmd == "diff":
        from graphify_plus.review.cli import main as diff_main
        return diff_main(rest)

    if cmd == "init":
        from graphify_plus.interface.cli.init_cmd import init_cmd
        # Click commands are callable: standalone_mode=False bubbles errors
        try:
            init_cmd.main(args=rest, prog_name="graphify-plus init",
                          standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        return 0

    if cmd == "skeleton":
        from graphify_plus.interface.cli.skeleton_cmd import skeleton_cmd
        try:
            skeleton_cmd.main(args=rest, prog_name="graphify-plus skeleton",
                              standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "enhance":
        print(
            "graphify-plus enhance: not yet shipped in this release.\n"
            "Only `audit` and `diff` are production-ready as of v3.1.1.\n"
            "See README roadmap for `enhance` (v2 pipeline) status.",
            file=sys.stderr,
        )
        return 2

    print(f"graphify-plus: unknown command {cmd!r}", file=sys.stderr)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
