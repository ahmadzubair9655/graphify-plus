"""Layer 4.2 — session-scoped writeback that flows, doesn't interrupt.

Today's `annotate_node` is an explicit write tool. The new pattern:
every Edit/Write that Claude does triggers a passive proposal — "this
change touched node X. Should I annotate the graph with the rationale
you gave me?". The user approves once and it becomes implicit for
the rest of the session.

This module ships the *engine*. The actual passive trigger lives in a
Claude Code hook (Layer 4.2 in spirit; the hook script is in
``templates/post_edit_hook.py``).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("graphify_plus.daemon.writeback_proposals")

PROPOSALS_FILE = "writeback-proposals.jsonl"
SESSION_STATE_FILE = "writeback-session.json"


@dataclass
class Proposal:
    id: str
    target: str
    rationale: str
    edit_path: str
    edit_lines: tuple[int, int] = (0, 0)
    state: str = "pending"  # pending | accepted | rejected
    proposed_at: str = ""
    decided_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SessionState:
    auto_accept: bool = False
    started_at: str = ""


def proposals_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / PROPOSALS_FILE


def session_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / SESSION_STATE_FILE


def load_session(repo: Path) -> SessionState:
    p = session_path(repo)
    if not p.exists():
        return SessionState()
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SessionState()
    return SessionState(
        auto_accept=bool(body.get("auto_accept", False)),
        started_at=str(body.get("started_at", "")),
    )


def save_session(repo: Path, state: SessionState) -> None:
    p = session_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"auto_accept": state.auto_accept, "started_at": state.started_at}, indent=2),
        encoding="utf-8",
    )


def propose(
    repo: Path,
    *,
    target: str,
    rationale: str,
    edit_path: str = "",
    edit_lines: tuple[int, int] = (0, 0),
) -> Proposal:
    """Append a new proposal to the session stream. Idempotent on
    (target, rationale).
    """
    pid = hashlib.sha256(f"{target}::{rationale}".encode()).hexdigest()[:12]
    rec = Proposal(
        id=pid,
        target=target,
        rationale=rationale,
        edit_path=edit_path,
        edit_lines=edit_lines,
        state="pending",
        proposed_at=datetime.now(timezone.utc).isoformat(),
    )
    p = proposals_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Idempotent — skip if (id, state=pending) already present.
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                if row.get("id") == pid and row.get("state") in ("pending", "accepted"):
                    return Proposal(**row)
            except (json.JSONDecodeError, TypeError):
                continue
    state = load_session(repo)
    if state.auto_accept:
        rec.state = "accepted"
        rec.decided_at = rec.proposed_at
    with p.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(rec.to_dict(), separators=(",", ":")) + "\n")
    return rec


def list_proposals(repo: Path, *, state: str | None = None) -> list[Proposal]:
    p = proposals_path(repo)
    if not p.exists():
        return []
    rows: list[Proposal] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if state and row.get("state") != state:
                    continue
                rows.append(Proposal(**row))
            except (json.JSONDecodeError, TypeError):
                continue
    except OSError:
        return []
    return rows


def decide(repo: Path, proposal_id: str, *, accept: bool) -> Proposal | None:
    """Append a decision record (we never rewrite history)."""
    rows = list_proposals(repo)
    target = next((r for r in rows if r.id == proposal_id), None)
    if target is None:
        return None
    target.state = "accepted" if accept else "rejected"
    target.decided_at = datetime.now(timezone.utc).isoformat()
    p = proposals_path(repo)
    with p.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(target.to_dict(), separators=(",", ":")) + "\n")
    return target


def enable_auto_accept(repo: Path) -> SessionState:
    state = load_session(repo)
    state.auto_accept = True
    state.started_at = state.started_at or datetime.now(timezone.utc).isoformat()
    save_session(repo, state)
    return state


def disable_auto_accept(repo: Path) -> SessionState:
    state = load_session(repo)
    state.auto_accept = False
    save_session(repo, state)
    return state


__all__ = [
    "Proposal",
    "PROPOSALS_FILE",
    "SESSION_STATE_FILE",
    "SessionState",
    "decide",
    "disable_auto_accept",
    "enable_auto_accept",
    "list_proposals",
    "load_session",
    "proposals_path",
    "propose",
    "save_session",
    "session_path",
]
