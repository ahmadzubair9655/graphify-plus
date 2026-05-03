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
import os
import sys
from pathlib import Path

import click

from graphify_plus.interface.errors import GraphifyError


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
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON instead of text"
    )
    parser.add_argument(
        "--output", "-o", metavar="PATH", help="Write result to file instead of stdout"
    )
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
        audit_meets_threshold,
        format_audit_report,
        run_audit,
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
    # Phase 11: structured error handling. --debug flag (or GP_DEBUG=1) flips
    # full traceback printing; otherwise stderr stays clean and the trace
    # lands in <repo>/.graphify_plus/debug.log.
    debug = "--debug" in rest or os.environ.get("GP_DEBUG") == "1"
    if "--debug" in rest:
        rest = [a for a in rest if a != "--debug"]
    try:
        return _dispatch(cmd, rest)
    except GraphifyError as err:
        from graphify_plus.interface.errors import handle as _handle_err

        return _handle_err(err, debug=debug)
    except KeyboardInterrupt:
        return 130
    except Exception as err:  # noqa: BLE001 — top-level safety net
        from graphify_plus.interface.errors import handle as _handle_err

        return _handle_err(err, debug=debug)


def _dispatch(cmd: str, rest: list[str]) -> int:

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
            init_cmd.main(args=rest, prog_name="graphify-plus init", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        return 0

    if cmd == "doctor":
        from graphify_plus.interface.cli.doctor_cmd import doctor_cmd

        try:
            doctor_cmd.main(args=rest, prog_name="graphify-plus doctor", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "claude-md":
        from graphify_plus.interface.cli.claude_md_cmd import claude_md_cmd

        try:
            claude_md_cmd.main(
                args=rest, prog_name="graphify-plus claude-md", standalone_mode=False
            )
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "mcp":
        from graphify_plus.interface.mcp_server import main as mcp_main

        return mcp_main()

    if cmd in ("matrix", "visual"):
        from graphify_plus.interface.cli.matrix_cmd import (
            matrix_cmd,
            visual_cmd,
        )

        click_cmd_map = {"matrix": matrix_cmd, "visual": visual_cmd}
        try:
            click_cmd_map[cmd].main(
                args=rest, prog_name=f"graphify-plus {cmd}", standalone_mode=False
            )
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd in ("enrich", "blast-radius"):
        from graphify_plus.interface.cli.enrich_cmd import (
            blast_cmd,
            enrich_cmd,
        )

        click_cmd_map = {"enrich": enrich_cmd, "blast-radius": blast_cmd}
        try:
            click_cmd_map[cmd].main(
                args=rest, prog_name=f"graphify-plus {cmd}", standalone_mode=False
            )
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd in ("stitch", "coordinate"):
        from graphify_plus.interface.cli.coordinate_cmd import (
            coordinate_cmd,
            stitch_cmd,
        )

        click_cmd_map = {"stitch": stitch_cmd, "coordinate": coordinate_cmd}
        try:
            click_cmd_map[cmd].main(
                args=rest, prog_name=f"graphify-plus {cmd}", standalone_mode=False
            )
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd in ("simulate", "guardrails", "drift"):
        from graphify_plus.interface.cli.safety_cmds import (
            drift_cmd,
            guardrails_cmd,
            simulate_cmd,
        )

        click_cmd = {"simulate": simulate_cmd, "guardrails": guardrails_cmd, "drift": drift_cmd}[
            cmd
        ]
        try:
            click_cmd.main(args=rest, prog_name=f"graphify-plus {cmd}", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "plan":
        from graphify_plus.interface.cli.plan_cmd import plan_cmd

        try:
            plan_cmd.main(args=rest, prog_name="graphify-plus plan", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "sync-docs":
        from graphify_plus.interface.cli.sync_docs_cmd import sync_docs_cmd

        try:
            sync_docs_cmd.main(
                args=rest, prog_name="graphify-plus sync-docs", standalone_mode=False
            )
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "find":
        from graphify_plus.interface.cli.find_cmd import find_cmd

        try:
            find_cmd.main(args=rest, prog_name="graphify-plus find", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "prune":
        from graphify_plus.interface.cli.prune_cmd import prune_cmd

        try:
            prune_cmd.main(args=rest, prog_name="graphify-plus prune", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "vacuum":
        from graphify_plus.interface.cli.vacuum_cmd import vacuum_cmd

        try:
            vacuum_cmd.main(args=rest, prog_name="graphify-plus vacuum", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "explain":
        from graphify_plus.interface.cli.explain_cmd import explain_cmd

        try:
            explain_cmd.main(args=rest, prog_name="graphify-plus explain", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "context":
        from graphify_plus.interface.cli.context_cmd import context_cmd

        try:
            context_cmd.main(args=rest, prog_name="graphify-plus context", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "watch":
        from graphify_plus.interface.cli.watch_cmd import watch_cmd

        try:
            watch_cmd.main(args=rest, prog_name="graphify-plus watch", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "session":
        from graphify_plus.interface.cli.session_cmd import session_cmd

        try:
            session_cmd.main(args=rest, prog_name="graphify-plus session", standalone_mode=False)
        except SystemExit as e:
            return int(e.code or 0)
        except click.ClickException as e:
            e.show()
            return 1
        return 0

    if cmd == "skeleton":
        from graphify_plus.interface.cli.skeleton_cmd import skeleton_cmd

        try:
            skeleton_cmd.main(args=rest, prog_name="graphify-plus skeleton", standalone_mode=False)
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
