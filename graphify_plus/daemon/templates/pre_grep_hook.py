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
    if not pattern:
        return 0

    cwd = Path(event.get("cwd") or ".").resolve()
    is_bareword = bool(BAREWORD.match(pattern))

    suggestion: str | None = None
    daemon_running = False
    fresh = False
    matched = 0
    if is_bareword:
        suggestion, daemon_running, fresh, matched = _suggest_with_diagnostics(cwd, pattern)

    # Adoption telemetry: record EVERY grep we see (bareword or not),
    # regardless of whether we nudge. The denominator matters as much
    # as the numerator. Layer-4.3 / review-guide-recommended.
    _record_event(
        cwd,
        pattern=pattern,
        nudged=suggestion is not None,
        bareword=is_bareword,
        daemon_running=daemon_running,
        fresh=fresh,
        matched_node_count=matched,
    )

    if suggestion is None:
        return 0
    out = {
        "decision": "approve",
        "reason": suggestion,
        "additionalContext": suggestion,
    }
    json.dump(out, sys.stdout)
    return 0


def _record_event(
    repo: Path,
    *,
    pattern: str,
    nudged: bool,
    bareword: bool,
    daemon_running: bool,
    fresh: bool,
    matched_node_count: int,
) -> None:
    try:
        from graphify_plus.daemon.adoption import record_grep_event

        record_grep_event(
            repo,
            pattern=pattern,
            nudged=nudged,
            bareword=bareword,
            daemon_running=daemon_running,
            fresh=fresh,
            matched_node_count=matched_node_count,
        )
    except Exception:  # noqa: BLE001
        pass


def _suggest_with_diagnostics(repo: Path, name: str) -> tuple[str | None, bool, bool, int]:
    """Returns ``(suggestion, daemon_running, fresh, matched_count)``."""
    try:
        from graphify_plus.daemon.client import DaemonClient
    except Exception:  # noqa: BLE001
        return None, False, False, 0
    client = DaemonClient(repo, timeout_s=0.5)
    if not client.is_running():
        return None, False, False, 0
    try:
        resp = client.call("find_by_name", {"label": name, "budget_tokens": 600})
    except Exception:  # noqa: BLE001
        return None, True, False, 0
    fresh_dict = resp.get("freshness", {})
    is_fresh = fresh_dict.get("trust") in ("FRESH", "LIVE_AHEAD")
    rows = resp.get("results", [])
    if not rows or not is_fresh:
        return None, True, is_fresh, len(rows)
    top = rows[0]
    label = top.get("label") or name
    file_line = f"{top.get('source_file', '?')}:{top.get('line_number', 0)}"
    n = len(rows)
    suggestion = (
        f"[graphify-plus hook] graph has {n} symbol(s) matching {name!r} "
        f"(top: {label} at {file_line}). "
        f"Try `gp_who_calls` / `gp_whats_in` / `gp_find_by_name` for "
        f"structural follow-ups — usually fewer round-trips than grep."
    )
    return suggestion, True, True, n


# Backwards-compatible wrapper kept for tests + external callers.
def _suggest(repo: Path, name: str) -> str | None:
    suggestion, _running, _fresh, _matched = _suggest_with_diagnostics(repo, name)
    return suggestion


if __name__ == "__main__":
    sys.exit(main())
