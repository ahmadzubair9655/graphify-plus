"""SQLite (WAL) cache layer.

Lives at ``<repo_root>/.graphify_plus/cache.db``. Used by every later phase:
Phase 1 (skeletons), Phase 2 (sessions), Phase 3 (centrality cache),
Phase 4 (community cache).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import orjson

from ..core.adapters import Edge, Symbol

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

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---- symbols & edges ------------------------------------------------
    def replace_all(self, symbols: Iterable[Symbol], edges: Iterable[Edge]) -> None:
        with self.tx():
            self.conn.execute("DELETE FROM symbols")
            self.conn.execute("DELETE FROM edges")
            self.conn.executemany(
                "INSERT INTO symbols(id, json) VALUES (?, ?)",
                [(s["id"], orjson.dumps(s, option=orjson.OPT_SORT_KEYS)) for s in symbols],
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
