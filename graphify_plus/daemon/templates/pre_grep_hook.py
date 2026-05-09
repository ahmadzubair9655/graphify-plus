#!/usr/bin/env python3
"""Claude Code PreToolUse hook for Grep / Glob calls.

Wire into ``~/.claude/settings.json`` (or the repo's
``.claude/settings.json``) under ``hooks.PreToolUse`` like::

    "PreToolUse": [
      {
        "matcher": "Grep|Glob",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/repo/.claude/hooks/pre_grep_hook.py"
          }
        ]
      }
    ]

The hook reads a JSON event from stdin and writes a JSON response to
stdout. It NEVER blocks the grep — at most it appends an advisory line
suggesting a graph tool that would answer the same question with fewer
round-trips. Stale graphs stay quiet (Layer 3.2 of the master plan:
"stale graphs have lost the right to advise").

The matching logic is deliberately conservative: we only nudge when the
pattern is a single bareword (``[A-Za-z_][A-Za-z0-9_]*``) AND the daemon
returns at least one structural hit for that name. Regex queries,
filesystem glob queries, multi-file queries — all pass through silently.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BAREWORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # silently pass through

    tool_name = event.get("tool_name") or event.get("toolName") or ""
    if tool_name not in ("Grep", "Glob"):
        return 0

    pattern = (event.get("tool_input") or {}).get("pattern") or ""
    if not pattern or not BAREWORD.match(pattern):
        return 0

    cwd = Path(event.get("cwd") or ".").resolve()
    suggestion = _suggest(cwd, pattern)
    if suggestion is None:
        return 0

    out = {
        "decision": "approve",
        "reason": suggestion,
        "additionalContext": suggestion,
    }
    json.dump(out, sys.stdout)
    return 0


def _suggest(repo: Path, name: str) -> str | None:
    """Returns a one-line hint or None. Never raises."""
    try:
        from graphify_plus.daemon.client import DaemonClient
    except Exception:  # noqa: BLE001
        return None
    client = DaemonClient(repo, timeout_s=0.5)
    if not client.is_running():
        return None
    try:
        resp = client.call("find_by_name", {"label": name, "budget_tokens": 600})
    except Exception:  # noqa: BLE001
        return None
    fresh = resp.get("freshness", {})
    if fresh.get("trust") not in ("FRESH", "LIVE_AHEAD"):
        # Stale graphs lose the right to advise.
        return None
    rows = resp.get("results", [])
    if not rows:
        return None
    top = rows[0]
    label = top.get("label") or name
    file_line = f"{top.get('source_file', '?')}:{top.get('line_number', 0)}"
    n = len(rows)
    return (
        f"[graphify-plus hook] graph has {n} symbol(s) matching {name!r} "
        f"(top: {label} at {file_line}). "
        f"Try `gp_who_calls` / `gp_whats_in` / `gp_find_by_name` for "
        f"structural follow-ups — usually fewer round-trips than grep."
    )


if __name__ == "__main__":
    sys.exit(main())
