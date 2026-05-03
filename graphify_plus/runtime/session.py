"""Delta-Memory: per-session record of which skeleton hashes the AI
already received, so subsequent ``gp context`` queries can ship deltas
instead of full re-sends.

Backed by the ``sessions`` table introduced in Phase 0 (with ``created_at``
column added here). Sessions auto-expire after 24 hours.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from .store import Store

SESSION_TTL_SEC = 24 * 60 * 60


def _ensure_schema(store: Store) -> None:
    """Add timestamp column to sessions table if missing (one-shot migration)."""
    cols = {row[1] for row in store.conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "created_at" not in cols:
        store.conn.execute("ALTER TABLE sessions ADD COLUMN created_at INTEGER DEFAULT 0")


@dataclass
class Session:
    """A token-saving session. Records which skeleton hashes the AI has
    already seen in this conversation."""

    id: str
    store: Store

    # ---- factory --------------------------------------------------------
    @classmethod
    def start(cls, store: Store, session_id: str | None = None) -> Session:
        _ensure_schema(store)
        sid = session_id or str(uuid.uuid4())
        # Create a sentinel row so the session exists even before any touch.
        store.conn.execute(
            "INSERT OR IGNORE INTO sessions(id, hash, created_at) VALUES(?, ?, ?)",
            (sid, "__sentinel__", int(time.time())),
        )
        return cls(id=sid, store=store)

    # ---- recording ------------------------------------------------------
    def touch(self, skeleton_hash: str) -> None:
        self.store.conn.execute(
            "INSERT OR IGNORE INTO sessions(id, hash, created_at) VALUES(?, ?, ?)",
            (self.id, skeleton_hash, int(time.time())),
        )

    def touch_many(self, hashes: Iterable[str]) -> None:
        rows = [(self.id, h, int(time.time())) for h in hashes]
        if rows:
            self.store.conn.executemany(
                "INSERT OR IGNORE INTO sessions(id, hash, created_at) VALUES(?, ?, ?)",
                rows,
            )

    # ---- delta ----------------------------------------------------------
    def delta(self, needed: Iterable[str]) -> tuple[set[str], set[str]]:
        """Return ``(already_seen, new)`` for the requested skeleton hashes."""
        wanted = set(needed)
        if not wanted:
            return set(), set()
        placeholders = ",".join(["?"] * len(wanted))
        rows = self.store.conn.execute(
            f"SELECT hash FROM sessions WHERE id = ? AND hash IN ({placeholders})",
            (self.id, *wanted),
        ).fetchall()
        seen = {r[0] for r in rows}
        return seen, wanted - seen

    def status(self) -> dict[str, int | str]:
        row = self.store.conn.execute(
            "SELECT COUNT(*) - 1, MIN(created_at) FROM sessions WHERE id = ?",
            (self.id,),
        ).fetchone()
        return {
            "id": self.id,
            "hashes_seen": max(0, int(row[0] or 0)),
            "started_at": int(row[1] or 0),
        }


def prune_expired(store: Store, *, ttl_sec: int = SESSION_TTL_SEC) -> int:
    _ensure_schema(store)
    cutoff = int(time.time()) - ttl_sec
    cur = store.conn.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
    return cur.rowcount or 0


__all__ = ["Session", "prune_expired"]
