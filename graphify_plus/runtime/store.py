"""SQLite (WAL) cache layer.

Lives at ``<repo_root>/.graphify_plus/cache.db``. Used by every later phase:
Phase 1 (skeletons), Phase 2 (sessions), Phase 3 (centrality cache),
Phase 4 (community cache).

Phase 11 hardens this layer:

  - Integrity check (``PRAGMA quick_check``) on every open. On failure
    the file is renamed to ``cache.db.corrupt.<utc-timestamp>`` and a
    ``GraphifyError(code=GP-CACHE-CORRUPT)`` is raised so the CLI can
    surface remediation.
  - Schema versioning via the ``meta`` table. Mismatches raise
    ``GP-CACHE-VERSION-MISMATCH``.
  - Backup-before-destructive: ``backup()`` copies ``cache.db`` →
    ``cache.db.bak`` before ``init --force`` / migrations.
  - Atomic writes via ``tx()`` (BEGIN IMMEDIATE) with bounded
    exponential-backoff retry on ``SQLITE_BUSY``. Exhaustion raises
    ``GP-CACHE-LOCKED``.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import orjson

log = logging.getLogger("graphify_plus.runtime.store")

from ..core.adapters import Edge, Symbol
from ..interface.errors import (
    CacheCorrupt,
    CacheLocked,
    CacheVersionMismatch,
)

SCHEMA_VERSION = 1
RETRY_DELAYS_S = (0.1, 0.2, 0.4, 0.8, 1.6)  # 5 attempts before GP-CACHE-LOCKED

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id    TEXT PRIMARY KEY,
    json  BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS edges (
    rowid INTEGER PRIMARY KEY,
    src   TEXT NOT NULL,
    dst   TEXT NOT NULL,
    kind  TEXT NOT NULL,
    json  BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS skeletons (
    hash TEXT PRIMARY KEY,
    body BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id   TEXT NOT NULL,
    hash TEXT NOT NULL,
    PRIMARY KEY (id, hash)
);
CREATE TABLE IF NOT EXISTS symbol_skeletons (
    symbol_id TEXT PRIMARY KEY,
    hash      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbol_skeletons_hash ON symbol_skeletons(hash);
"""


def cache_path(repo_root: Path) -> Path:
    return repo_root / ".graphify_plus" / "cache.db"


class Store:
    """Thin wrapper around the SQLite cache. WAL mode, sync=NORMAL.

    Not thread-safe; callers should keep one Store per thread.
    """

    def __init__(self, db_path: Path, *, integrity_check: bool = True):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA temp_store=MEMORY")
            # Reasonable default lock timeout (5s) — tx() does its own retry too.
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
        except sqlite3.DatabaseError as e:
            self._quarantine(reason=f"open-time DatabaseError: {e}")
            raise CacheCorrupt(
                f"cache.db at {self.db_path} could not be opened",
                context={"path": str(self.db_path), "stage": "init", "error": str(e)},
            ) from e
        if integrity_check:
            self._verify_integrity()
        self._verify_schema_version()

    # ---- integrity & versioning ----------------------------------------
    def _verify_integrity(self) -> None:
        try:
            row = self.conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as e:
            self._quarantine(reason=f"DatabaseError: {e}")
            raise CacheCorrupt(
                f"cache.db at {self.db_path} could not be opened",
                context={"path": str(self.db_path)},
            ) from e
        if not row or row[0] != "ok":
            self._quarantine(reason=f"quick_check={row[0] if row else None}")
            raise CacheCorrupt(
                f"cache.db at {self.db_path} failed PRAGMA quick_check",
                context={"path": str(self.db_path), "result": row[0] if row else None},
            )

    def _verify_schema_version(self) -> None:
        # Bootstrap: write SCHEMA_VERSION on first open.
        existing = self.get_meta("schema_version")
        if existing is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            return
        try:
            on_disk = int(existing)
        except (TypeError, ValueError):
            on_disk = 0
        if on_disk == SCHEMA_VERSION:
            return
        if on_disk > SCHEMA_VERSION:
            raise CacheVersionMismatch(
                f"cache schema_version={on_disk} is newer than tool's "
                f"SCHEMA_VERSION={SCHEMA_VERSION}",
                context={"path": str(self.db_path), "on_disk": on_disk, "tool": SCHEMA_VERSION},
            )
        # on_disk < SCHEMA_VERSION → we'd run migrations here. v4 ships
        # one schema version; future migrations land via runtime/migrations/.
        raise CacheVersionMismatch(
            f"cache schema_version={on_disk} is older than tool's "
            f"SCHEMA_VERSION={SCHEMA_VERSION} and no migration is registered",
            context={"path": str(self.db_path), "on_disk": on_disk, "tool": SCHEMA_VERSION},
        )

    def _quarantine(self, *, reason: str) -> None:
        """Move the corrupt file aside so a fresh init can proceed."""
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = self.db_path.with_suffix(f".db.corrupt.{ts}")
        try:
            shutil.move(str(self.db_path), str(bak))
        except OSError:
            pass

    # ---- backup --------------------------------------------------------
    def backup(self) -> Path | None:
        """Copy cache.db → cache.db.bak (rolling single backup). Returns
        the backup path, or None if the cache file isn't present yet.
        """
        if not self.db_path.exists():
            return None
        bak = self.db_path.with_suffix(".db.bak")
        # SQLite-aware: use the backup API to handle WAL correctly.
        dst = sqlite3.connect(bak)
        try:
            self.conn.backup(dst)
        finally:
            dst.close()
        return bak

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE with bounded exponential-backoff retry on
        SQLITE_BUSY. After exhausting RETRY_DELAYS_S, raises CacheLocked.
        """
        last_err: Exception | None = None
        for _attempt, delay in enumerate((0.0, *RETRY_DELAYS_S)):
            if delay:
                time.sleep(delay)
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                last_err = e
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                continue
        else:
            raise CacheLocked(
                f"could not acquire write lock on {self.db_path} after "
                f"{len(RETRY_DELAYS_S)} retries",
                context={"path": str(self.db_path), "last_error": str(last_err)},
            )
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    # ---- symbols & edges ------------------------------------------------
    def replace_all(self, symbols: Iterable[Symbol], edges: Iterable[Edge]) -> None:
        # Dedupe symbols by id (last occurrence wins) so callers feeding
        # multi-pass extractor output don't blow up on the UNIQUE(id)
        # constraint. Duplicate IDs typically signal an upstream extractor
        # bug (TS overload, default-export collision, two adapters claiming
        # the same file) — log a warning with a sample so it stays
        # discoverable in debug.log instead of silently being collapsed.
        unique_symbols: dict[str, Symbol] = {}
        dup_count = 0
        dup_samples: list[str] = []
        for s in symbols:
            sid = s["id"]
            if sid in unique_symbols:
                dup_count += 1
                if len(dup_samples) < 5:
                    dup_samples.append(sid)
            unique_symbols[sid] = s
        if dup_count:
            log.warning(
                "replace_all: dropped %d duplicate symbol id(s); samples=%s",
                dup_count,
                dup_samples,
            )

        with self.tx():
            self.conn.execute("DELETE FROM symbols")
            self.conn.execute("DELETE FROM edges")
            self.conn.executemany(
                "INSERT INTO symbols(id, json) VALUES (?, ?)",
                [
                    (sid, orjson.dumps(s, option=orjson.OPT_SORT_KEYS))
                    for sid, s in unique_symbols.items()
                ],
            )
            self.conn.executemany(
                "INSERT INTO edges(src, dst, kind, json) VALUES (?, ?, ?, ?)",
                [
                    (
                        e["src"],
                        e["dst"],
                        e.get("kind") or "",  # type: ignore[typeddict-item]
                        orjson.dumps(e, option=orjson.OPT_SORT_KEYS),
                    )
                    for e in edges
                ],
            )

    @staticmethod
    def _decode_symbol(raw: bytes) -> Symbol:
        s = orjson.loads(raw)
        if isinstance(s.get("span"), list):
            s["span"] = tuple(s["span"])  # type: ignore[typeddict-item]
        return s  # type: ignore[return-value]

    @staticmethod
    def _decode_edge(raw: bytes) -> Edge:
        e = orjson.loads(raw)
        if isinstance(e.get("span"), list):
            e["span"] = tuple(e["span"])  # type: ignore[typeddict-item]
        return e  # type: ignore[return-value]

    def all_symbols(self) -> list[Symbol]:
        rows = self.conn.execute("SELECT json FROM symbols ORDER BY id").fetchall()
        return [self._decode_symbol(r[0]) for r in rows]

    def all_edges(self) -> list[Edge]:
        rows = self.conn.execute("SELECT json FROM edges ORDER BY src, dst, kind, rowid").fetchall()
        return [self._decode_edge(r[0]) for r in rows]

    def get_symbol(self, sid: str) -> Symbol | None:
        row = self.conn.execute("SELECT json FROM symbols WHERE id = ?", (sid,)).fetchone()
        return self._decode_symbol(row[0]) if row else None

    # ---- meta -----------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # ---- skeletons (filled by Phase 1) ---------------------------------
    def put_skeleton(self, h: str, body: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO skeletons(hash, body) VALUES(?, ?)",
            (h, body.encode("utf-8")),
        )

    def get_skeleton(self, h: str) -> str | None:
        row = self.conn.execute("SELECT body FROM skeletons WHERE hash=?", (h,)).fetchone()
        return row[0].decode("utf-8") if row else None

    # ---- symbol → skeleton mapping --------------------------------------
    def link_skeleton(self, symbol_id: str, skeleton_hash: str) -> None:
        self.conn.execute(
            "INSERT INTO symbol_skeletons(symbol_id, hash) VALUES(?, ?) "
            "ON CONFLICT(symbol_id) DO UPDATE SET hash=excluded.hash",
            (symbol_id, skeleton_hash),
        )

    def link_skeletons_bulk(self, mapping: Iterable[tuple[str, str]]) -> None:
        self.conn.executemany(
            "INSERT INTO symbol_skeletons(symbol_id, hash) VALUES(?, ?) "
            "ON CONFLICT(symbol_id) DO UPDATE SET hash=excluded.hash",
            list(mapping),
        )

    def get_skeleton_for_symbol(self, symbol_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT body FROM symbol_skeletons s JOIN skeletons k ON s.hash = k.hash "
            "WHERE s.symbol_id = ?",
            (symbol_id,),
        ).fetchone()
        return row[0].decode("utf-8") if row else None

    def find_symbols_by_qualified_name(self, qname: str) -> list[Symbol]:
        """Return symbols matching ``qname``.

        Match priority: exact qualified_name, then exact name, then
        suffix-match on qualified_name (so 'Invoice.total' finds
        'invoice.Invoice.total'). Linear scan — Phase 4 will index.
        """
        rows = self.conn.execute("SELECT json FROM symbols").fetchall()
        exact: list[Symbol] = []
        suffix: list[Symbol] = []
        suffix_key = "." + qname
        for r in rows:
            s = self._decode_symbol(r[0])
            if s.get("qualified_name") == qname or s.get("name") == qname:
                exact.append(s)
            elif (s.get("qualified_name") or "").endswith(suffix_key):
                suffix.append(s)
        return exact or suffix

    def symbols_in_path(self, path: str) -> list[Symbol]:
        rows = self.conn.execute("SELECT json FROM symbols").fetchall()
        out: list[Symbol] = []
        for r in rows:
            s = self._decode_symbol(r[0])
            if s.get("path") == path:
                out.append(s)
        out.sort(key=lambda s: (s.get("span") or (0, 0))[0])
        return out


__all__ = ["Store", "cache_path"]
