"""Cross-stack edge synthesis — Layer 8 of the master plan.

Today's graph is per-language. The interesting questions cross those
boundaries: a Python ``@app.post("/api/users")`` and a TypeScript
``fetch("/api/users", { method: "POST" })`` are two halves of one edge.
Same for ORM models and SQL tables.

This module synthesises those edges *post-ingest*, reading the existing
symbol set and emitting new ``cross_stack`` edges. They live in their
own SQLite table so a re-sync is cheap (``DELETE FROM cross_edges WHERE
kind=…``).

Layer 8.1 — HTTP boundary edges
-------------------------------
Endpoint regexes that catch the most common Python / Node frameworks:

  * Flask / FastAPI / Quart     ``@app.route(...) / @app.get(...) / @router.post(...)``
  * Express / Koa / Fastify     ``app.get('/path', ...) / router.post('/path', ...)``
  * Django                      ``path('api/users', view) / re_path(...)``

Frontend call regexes:

  * ``fetch('/api/users', {method: 'POST'})``
  * ``axios.get('/api/users')``
  * ``$.ajax({url: '/api/users'})``

For each ``(method, url)`` pair we link callers to definers. Methods
match modulo HTTP verb (GET in client matches GET in backend).

Layer 8.2 — DB schema edges
---------------------------
We don't try to parse arbitrary SQL — that's its own project. Instead:

  * Walk ``.sql`` files for ``CREATE TABLE``/``ALTER TABLE`` statements,
    emit one ``schema:<table>`` node per table.
  * Walk ORM models (Django ``models.Model`` / SQLAlchemy
    ``declarative_base()`` / Prisma) — already emitted by the language
    adapters as classes — and link them to schema nodes by table-name
    match (``Meta.db_table``, ``__tablename__``, or the class name in
    snake_case).

These are heuristic, deliberately. They produce useful edges in 80% of
cases and stay quiet in the 20% they don't recognise — which is the
right contract for a feature that augments other tools rather than
replacing them.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.adapters import Symbol
from ..runtime.store import Store

log = logging.getLogger("graphify_plus.daemon.cross_stack")


CROSS_EDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cross_edges (
    rowid     INTEGER PRIMARY KEY,
    src       TEXT NOT NULL,
    dst       TEXT NOT NULL,
    kind      TEXT NOT NULL,        -- 'http' | 'db'
    detail    TEXT,                 -- JSON
    source    TEXT,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cross_src ON cross_edges(src);
CREATE INDEX IF NOT EXISTS idx_cross_dst ON cross_edges(dst);
CREATE INDEX IF NOT EXISTS idx_cross_kind ON cross_edges(kind);
"""

# ---- HTTP detection ------------------------------------------------------

# Backend: server-side route definitions. Each pattern captures (verb, url).
BACKEND_PATTERNS = [
    # Flask / FastAPI / Starlette / Quart — verb-specific
    re.compile(
        r"@\w+\.(get|post|put|delete|patch|head|options)\(\s*[\"']([^\"']+)[\"']", re.IGNORECASE
    ),
    # Flask @app.route('/x', methods=['POST'])
    re.compile(r"@\w+\.route\(\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
    # Express / Koa / Fastify
    re.compile(
        r"\b(?:app|router|server)\s*\.\s*(get|post|put|delete|patch|head|options|all)\s*\(\s*[\"']([^\"']+)[\"']",
        re.IGNORECASE,
    ),
    # Django path('api/users', view)
    re.compile(r"\b(?:re_)?path\(\s*[\"']([^\"']+)[\"']"),
]

# Frontend: client-side calls. Capture (verb-or-empty, url).
FRONTEND_PATTERNS = [
    # fetch('/x', {method: 'POST'}) — verb extracted separately
    re.compile(
        r"fetch\(\s*[\"'`]([^\"'`]+)[\"'`]\s*,?\s*(\{[^)]{0,200}\})?",
    ),
    # axios.get('/x')
    re.compile(
        r"\baxios\s*\.\s*(get|post|put|delete|patch|head)\s*\(\s*[\"'`]([^\"'`]+)[\"'`]",
        re.IGNORECASE,
    ),
    # $.ajax({url: '/x'})
    re.compile(r"\$\.ajax\(\s*\{[^}]*?url\s*:\s*[\"']([^\"']+)[\"']"),
    # $.get('/x'), $.post('/x')
    re.compile(r"\$\.(get|post|put|delete)\(\s*[\"']([^\"']+)[\"']", re.IGNORECASE),
]

METHOD_IN_FETCH = re.compile(r"method\s*:\s*[\"'`](\w+)[\"'`]", re.IGNORECASE)


@dataclass
class HTTPEndpoint:
    method: str
    url: str
    symbol_id: str
    file: str
    line: int


def _normalise_url(url: str) -> str:
    """Drop trailing slashes and route params for matching."""
    u = url.rstrip("/").lower()
    # Replace `:id` / `<int:id>` / `${name}` with `*` so `/users/:id`
    # matches `/users/${userId}`.
    u = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "*", u)
    u = re.sub(r"<[^>]+>", "*", u)
    u = re.sub(r"\$\{[^}]+\}", "*", u)
    return u


def detect_http_endpoints(
    symbols: list[Symbol], repo: Path
) -> tuple[list[HTTPEndpoint], list[HTTPEndpoint]]:
    """Walk symbol-bearing source files, return (backends, callers)."""
    backends: list[HTTPEndpoint] = []
    callers: list[HTTPEndpoint] = []
    by_path: dict[str, list[Symbol]] = {}
    for s in symbols:
        path = s.get("path") or ""
        if path:
            by_path.setdefault(path, []).append(s)
    for syms in by_path.values():
        syms.sort(key=lambda s: int((s.get("span") or (0, 0))[0]))
    for path, syms in by_path.items():
        full = repo / path
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _iter_backend(text):
            line = text[: m.start()].count("\n") + 1
            owner = _owner(syms, line)
            if owner:
                backends.append(
                    HTTPEndpoint(
                        method=m["method"],
                        url=_normalise_url(m["url"]),
                        symbol_id=owner["id"],
                        file=path,
                        line=line,
                    )
                )
        for m in _iter_frontend(text):
            line = text[: m.start()].count("\n") + 1
            owner = _owner(syms, line)
            if owner:
                callers.append(
                    HTTPEndpoint(
                        method=m["method"],
                        url=_normalise_url(m["url"]),
                        symbol_id=owner["id"],
                        file=path,
                        line=line,
                    )
                )
    return backends, callers


def _iter_backend(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pat in BACKEND_PATTERNS:
        for m in pat.finditer(text):
            groups = m.groups()
            if pat is BACKEND_PATTERNS[1]:  # @app.route('/x'), no verb
                out.append({"method": "ANY", "url": groups[0], "start": m.start()})
            elif pat is BACKEND_PATTERNS[3]:  # django path('x', ...)
                out.append({"method": "ANY", "url": groups[0], "start": m.start()})
            else:
                method = groups[0].upper() if len(groups) >= 2 else "ANY"
                url = groups[1] if len(groups) >= 2 else groups[0]
                out.append({"method": method, "url": url, "start": m.start()})

    # Synthetic .start() attribute so we can keep using m.start() in the caller.
    class _M:
        def __init__(self, d: dict[str, Any]):
            self.d = d

        def start(self) -> int:
            return self.d["start"]

        def __getitem__(self, k: str) -> Any:
            return self.d[k]

    return [_M(d) for d in out]  # type: ignore[return-value]


def _iter_frontend(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pat in FRONTEND_PATTERNS:
        for m in pat.finditer(text):
            groups = m.groups()
            method = "GET"
            url = ""
            if pat is FRONTEND_PATTERNS[0]:  # fetch
                url = groups[0]
                opts = groups[1] or ""
                mm = METHOD_IN_FETCH.search(opts)
                if mm:
                    method = mm.group(1).upper()
            elif pat is FRONTEND_PATTERNS[1]:  # axios.<verb>
                method = groups[0].upper()
                url = groups[1]
            elif pat is FRONTEND_PATTERNS[2]:  # $.ajax
                url = groups[0]
            elif pat is FRONTEND_PATTERNS[3]:  # $.<verb>
                method = groups[0].upper()
                url = groups[1]
            out.append({"method": method, "url": url, "start": m.start()})

    class _M:
        def __init__(self, d: dict[str, Any]):
            self.d = d

        def start(self) -> int:
            return self.d["start"]

        def __getitem__(self, k: str) -> Any:
            return self.d[k]

    return [_M(d) for d in out]  # type: ignore[return-value]


def _owner(syms: list[Symbol], line: int) -> Symbol | None:
    # Reverse — innermost containing symbol wins.
    candidate: Symbol | None = None
    smallest_size = float("inf")
    for s in syms:
        span = s.get("span") or (0, 0)
        a, b = int(span[0]), int(span[1])
        if a <= line <= b:
            size = b - a or 1_000_000
            if size < smallest_size:
                smallest_size = size
                candidate = s
    if candidate is None:
        # Fall back to the module symbol if any.
        for s in syms:
            if s.get("kind") == "module":
                return s
    return candidate


def synthesise_http_edges(
    backends: list[HTTPEndpoint], callers: list[HTTPEndpoint]
) -> list[dict[str, Any]]:
    """Match callers to backends on (method, url). ``ANY`` matches every
    HTTP verb. Returns one edge dict per match.
    """
    by_url: dict[str, list[HTTPEndpoint]] = {}
    for b in backends:
        by_url.setdefault(b.url, []).append(b)
    edges: list[dict[str, Any]] = []
    for c in callers:
        targets = by_url.get(c.url) or []
        # Wildcard route match: drop trailing path segments looking for parents.
        if not targets:
            for url, bs in by_url.items():
                if "*" in url and _wildcard_match(url, c.url):
                    targets = bs
                    break
        for b in targets:
            if b.method != "ANY" and c.method != "ANY" and b.method != c.method:
                continue
            edges.append(
                {
                    "src": c.symbol_id,
                    "dst": b.symbol_id,
                    "kind": "http",
                    "detail": {
                        "method": c.method,
                        "url": c.url,
                        "frontend_file": c.file,
                        "frontend_line": c.line,
                        "backend_file": b.file,
                        "backend_line": b.line,
                    },
                }
            )
    return edges


def _wildcard_match(template: str, path: str) -> bool:
    parts_t = template.split("/")
    parts_p = path.split("/")
    if len(parts_t) != len(parts_p):
        return False
    for t, p in zip(parts_t, parts_p, strict=False):
        if t == "*":
            continue
        if t != p:
            return False
    return True


# ---- DB schema ----------------------------------------------------------

CREATE_TABLE_RE = re.compile(
    r"CREATE\s+(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`']?(\w+)[\"`']?",
    re.IGNORECASE,
)
TABLENAME_PYATTR_RE = re.compile(r"__tablename__\s*=\s*[\"']([^\"']+)[\"']")
DJANGO_DB_TABLE_RE = re.compile(r"db_table\s*=\s*[\"']([^\"']+)[\"']")


@dataclass
class Table:
    name: str
    file: str = ""
    line: int = 0


def detect_db_tables(symbols: list[Symbol], repo: Path) -> list[Table]:
    seen: dict[str, Table] = {}
    for path in sorted({s.get("path") or "" for s in symbols if s.get("path")}):
        if not path.lower().endswith(".sql"):
            continue
        full = repo / path
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in CREATE_TABLE_RE.finditer(text):
            name = m.group(1).lower()
            if name in seen:
                continue
            line = text[: m.start()].count("\n") + 1
            seen[name] = Table(name=name, file=path, line=line)
    return list(seen.values())


def synthesise_db_edges(
    tables: list[Table], symbols: list[Symbol], repo: Path
) -> list[dict[str, Any]]:
    """Link ORM model classes to schema tables.

    Strategy: for each class symbol, look for ``__tablename__`` /
    ``Meta.db_table`` in its file body within the class's span. Fall
    back to ``snake_case(class_name)`` matching the schema table name.
    """
    by_name = {t.name: t for t in tables}
    edges: list[dict[str, Any]] = []
    by_path: dict[str, list[Symbol]] = {}
    for s in symbols:
        if s.get("kind") != "class":
            continue
        path = s.get("path") or ""
        if path:
            by_path.setdefault(path, []).append(s)
    for path, classes in by_path.items():
        full = repo / path
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
        except OSError:
            continue
        for cls in classes:
            span = cls.get("span") or (0, 0)
            a, b = int(span[0]), int(span[1])
            body = "\n".join(lines[max(a - 1, 0) : min(b, len(lines))])
            tablename: str | None = None
            m = TABLENAME_PYATTR_RE.search(body) or DJANGO_DB_TABLE_RE.search(body)
            if m:
                tablename = m.group(1).lower()
            else:
                tablename = _snake_case(cls.get("name") or "")
            if tablename and tablename in by_name:
                tbl = by_name[tablename]
                edges.append(
                    {
                        "src": cls["id"],
                        "dst": f"schema:{tbl.name}",
                        "kind": "db",
                        "detail": {
                            "table": tbl.name,
                            "table_file": tbl.file,
                            "table_line": tbl.line,
                            "model_class": cls.get("qualified_name"),
                            "model_file": cls.get("path"),
                            "model_line": int((cls.get("span") or (0, 0))[0]),
                        },
                    }
                )
    return edges


def _snake_case(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


# ---- store layer --------------------------------------------------------


def ensure_cross_table(store: Store) -> None:
    store.conn.executescript(CROSS_EDGE_SCHEMA)


def store_cross_edges(store: Store, edges: list[dict[str, Any]], *, kind: str) -> int:
    ensure_cross_table(store)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    with store.tx():
        store.conn.execute("DELETE FROM cross_edges WHERE kind = ?", (kind,))
        store.conn.executemany(
            "INSERT INTO cross_edges(src, dst, kind, detail, source, ingested_at) "
            "VALUES (?,?,?,?,?,?)",
            [
                (
                    e["src"],
                    e["dst"],
                    e["kind"],
                    json.dumps(e.get("detail") or {}),
                    e.get("source") or "",
                    now,
                )
                for e in edges
            ],
        )
    return len(edges)


def load_cross_edges(store: Store, kind: str | None = None) -> list[dict[str, Any]]:
    ensure_cross_table(store)
    if kind:
        rows = store.conn.execute(
            "SELECT src, dst, kind, detail FROM cross_edges WHERE kind = ?", (kind,)
        ).fetchall()
    else:
        rows = store.conn.execute("SELECT src, dst, kind, detail FROM cross_edges").fetchall()
    out: list[dict[str, Any]] = []
    for src, dst, k, detail in rows:
        try:
            d = json.loads(detail) if detail else {}
        except json.JSONDecodeError:
            d = {}
        out.append({"src": src, "dst": dst, "kind": k, "detail": d})
    return out


# ---- end-to-end ---------------------------------------------------------


def synthesise(store: Store, repo_root: Path) -> dict[str, Any]:
    """Walk the symbol set once, emit + persist HTTP and DB edges."""
    symbols = store.all_symbols()
    backends, callers = detect_http_endpoints(symbols, repo_root)
    http_edges = synthesise_http_edges(backends, callers)
    store_cross_edges(store, http_edges, kind="http")

    tables = detect_db_tables(symbols, repo_root)
    db_edges = synthesise_db_edges(tables, symbols, repo_root)
    store_cross_edges(store, db_edges, kind="db")

    return {
        "http_backends": len(backends),
        "http_callers": len(callers),
        "http_edges": len(http_edges),
        "db_tables": len(tables),
        "db_edges": len(db_edges),
    }


__all__ = [
    "CROSS_EDGE_SCHEMA",
    "HTTPEndpoint",
    "Table",
    "detect_db_tables",
    "detect_http_endpoints",
    "ensure_cross_table",
    "load_cross_edges",
    "store_cross_edges",
    "synthesise",
    "synthesise_db_edges",
    "synthesise_http_edges",
]
