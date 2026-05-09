"""External-source ingestors — Layer 6 of the master plan.

Each ingestor pulls data from an external system, normalises into
``(symbols, edges)`` tuples in the graphify-plus shape, and persists
into a new SQLite table that the daemon reads at build time.

* **6.1 GitHub** — issues + PRs via ``gh api``. Each becomes a node
  with ``kind="issue"`` / ``kind="pr"``. Edges link to symbols whose
  qualified_name appears in the issue body / PR description.
* **6.3 ADRs** — Architecture Decision Records. Walks a folder of
  Markdown files and extracts ``# Decision`` / ``# Status`` /
  ``# Consequences`` blocks. Each becomes a node with ``kind="adr"``;
  edges link to code symbols mentioned by name.

Storage uses a generic ``ingest_nodes`` table so we don't proliferate
type-specific tables. The ``InMemoryGraph`` reads it once at build
time and merges the rows into ``by_id`` with synthetic symbol IDs.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.adapters import Symbol
from ..runtime.store import Store

log = logging.getLogger("graphify_plus.daemon.ingestors")

INGEST_NODE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_nodes (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,        -- 'issue' | 'pr' | 'adr'
    title       TEXT NOT NULL,
    body        TEXT,                 -- truncated for storage
    source      TEXT NOT NULL,        -- 'github' | 'adr' | …
    url         TEXT,
    state       TEXT,                 -- 'open' | 'closed' | 'accepted' | …
    refs        TEXT,                 -- JSON array of symbol_ids this node mentions
    ingested_at TEXT NOT NULL,
    metadata    TEXT                  -- JSON blob of source-specific extras
);
CREATE INDEX IF NOT EXISTS idx_ingest_kind ON ingest_nodes(kind);
"""


@dataclass
class IngestNode:
    id: str
    kind: str  # 'issue' | 'pr' | 'adr'
    title: str
    body: str = ""
    source: str = ""
    url: str = ""
    state: str = ""
    refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def ensure_ingest_table(store: Store) -> None:
    store.conn.executescript(INGEST_NODE_SCHEMA)


def store_ingest_nodes(store: Store, rows: list[IngestNode], *, replace_kind: str) -> int:
    """Replace all ingest_nodes of the given kind with the new rows.

    Coverage / audit follow the same "replace" semantics — a re-ingest is
    authoritative. Mixing manual ADRs and GitHub issues is fine: each
    call only touches its declared kind.
    """
    ensure_ingest_table(store)
    now = datetime.now(timezone.utc).isoformat()
    with store.tx():
        store.conn.execute("DELETE FROM ingest_nodes WHERE kind = ?", (replace_kind,))
        store.conn.executemany(
            "INSERT INTO ingest_nodes(id, kind, title, body, source, url, state, refs, "
            "ingested_at, metadata) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    r.id,
                    r.kind,
                    r.title,
                    r.body[:4000],
                    r.source,
                    r.url,
                    r.state,
                    json.dumps(r.refs),
                    now,
                    json.dumps(r.metadata),
                )
                for r in rows
            ],
        )
    return len(rows)


def load_ingest_nodes(store: Store, kind: str | None = None) -> list[dict[str, Any]]:
    ensure_ingest_table(store)
    if kind:
        rows = store.conn.execute(
            "SELECT id, kind, title, body, source, url, state, refs, ingested_at, metadata "
            "FROM ingest_nodes WHERE kind = ?",
            (kind,),
        ).fetchall()
    else:
        rows = store.conn.execute(
            "SELECT id, kind, title, body, source, url, state, refs, ingested_at, metadata "
            "FROM ingest_nodes"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for nid, k, title, body, source, url, state, refs, ts, meta in rows:
        try:
            ref_list = json.loads(refs) if refs else []
        except json.JSONDecodeError:
            ref_list = []
        try:
            md = json.loads(meta) if meta else {}
        except json.JSONDecodeError:
            md = {}
        out.append(
            {
                "id": nid,
                "kind": k,
                "title": title,
                "body": body,
                "source": source,
                "url": url,
                "state": state,
                "refs": ref_list,
                "ingested_at": ts,
                "metadata": md,
            }
        )
    return out


# =========================================================================
# Layer 6.3 — ADR ingest
# =========================================================================

ADR_FRONT_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
ADR_STATUS_RE = re.compile(r"^#+\s*Status\s*\n+\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
ADR_FILENAME_NUM_RE = re.compile(r"^(\d{3,4})[-_]")


def parse_adr(path: Path) -> IngestNode | None:
    """Parse a single ADR markdown file into an `IngestNode`.

    Recognises the ADR convention loosely:

      * Filename like ``0042-use-postgres.md`` → id = ``adr-0042``
      * First ``# Title`` line → title
      * ``## Status`` → state (Accepted | Proposed | Rejected | Superseded)
      * Whole body — kept truncated for storage
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    title_match = ADR_FRONT_RE.search(text)
    if not title_match:
        return None
    title = title_match.group(1).strip()
    state = ""
    sm = ADR_STATUS_RE.search(text)
    if sm:
        state = sm.group(1).strip().splitlines()[0]
    fn_match = ADR_FILENAME_NUM_RE.match(path.name)
    adr_id = f"adr-{fn_match.group(1)}" if fn_match else f"adr-{path.stem}"
    return IngestNode(
        id=adr_id,
        kind="adr",
        title=title,
        body=text,
        source="adr",
        url=str(path),
        state=state,
    )


def parse_adr_folder(folder: Path) -> list[IngestNode]:
    rows: list[IngestNode] = []
    if not folder.exists() or not folder.is_dir():
        return rows
    for md in sorted(folder.rglob("*.md")):
        if md.name.lower() in ("readme.md", "index.md"):
            continue
        node = parse_adr(md)
        if node is not None:
            rows.append(node)
    return rows


def attribute_refs(nodes: list[IngestNode], symbols: list[Symbol]) -> None:
    """Best-effort: scan each node's body for symbol qualified-names and
    short names, populate ``refs`` with matching symbol_ids. In-place.
    """
    name_to_ids: dict[str, list[str]] = {}
    qname_to_id: dict[str, str] = {}
    for s in symbols:
        sid = s.get("id")
        if not sid:
            continue
        qn = (s.get("qualified_name") or "").lower()
        if qn:
            qname_to_id[qn] = sid
        name = (s.get("name") or "").lower()
        if name and len(name) >= 4:  # ignore noise like 'a', 'b'
            name_to_ids.setdefault(name, []).append(sid)
    for node in nodes:
        body_lower = (node.body or "").lower()
        seen: set[str] = set()
        for qn, sid in qname_to_id.items():
            if qn and qn in body_lower:
                seen.add(sid)
        # Match short names only when they look like code (preceded by
        # whitespace or punctuation, followed by `(`/`.`/space).
        for name, ids in name_to_ids.items():
            for m in re.finditer(rf"(?<![A-Za-z0-9_])({re.escape(name)})(?=[(.\s])", body_lower):
                seen.update(ids)
                break
        node.refs = sorted(seen)


def ingest_adr(store: Store, repo_root: Path, folder: Path) -> dict[str, Any]:
    """End-to-end: walk the folder, parse, attribute refs, persist."""
    nodes = parse_adr_folder(folder)
    if nodes:
        symbols = store.all_symbols()
        attribute_refs(nodes, symbols)
        store_ingest_nodes(store, nodes, replace_kind="adr")
    return {
        "files": len(nodes),
        "with_refs": sum(1 for n in nodes if n.refs),
    }


# =========================================================================
# Layer 6.1 — GitHub issue / PR ingest
# =========================================================================


def _gh(args: list[str], cwd: Path) -> str | None:
    """Run ``gh`` with the given args, return stdout or None on failure.

    The whole point of routing through ``gh`` is to inherit the user's
    auth — no separate token wrangling here.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("gh not available: %s", exc)
        return None
    if proc.returncode != 0:
        log.warning("gh failed: %s", proc.stderr.strip())
        return None
    return proc.stdout


def fetch_github_issues(repo_root: Path, *, state: str = "open", limit: int = 200) -> list[IngestNode]:
    out = _gh(
        [
            "issue",
            "list",
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            "number,title,body,state,url,labels,author,createdAt,updatedAt",
        ],
        repo_root,
    )
    if not out:
        return []
    try:
        body = json.loads(out)
    except json.JSONDecodeError:
        return []
    rows: list[IngestNode] = []
    for it in body:
        rows.append(
            IngestNode(
                id=f"gh-issue-{it['number']}",
                kind="issue",
                title=it.get("title") or "",
                body=it.get("body") or "",
                source="github",
                url=it.get("url") or "",
                state=it.get("state") or "",
                metadata={
                    "number": it.get("number"),
                    "labels": [
                        lab.get("name") for lab in (it.get("labels") or []) if isinstance(lab, dict)
                    ],
                    "author": (it.get("author") or {}).get("login"),
                    "created_at": it.get("createdAt"),
                    "updated_at": it.get("updatedAt"),
                },
            )
        )
    return rows


def fetch_github_prs(repo_root: Path, *, state: str = "all", limit: int = 200) -> list[IngestNode]:
    out = _gh(
        [
            "pr",
            "list",
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            "number,title,body,state,url,labels,author,createdAt,mergedAt",
        ],
        repo_root,
    )
    if not out:
        return []
    try:
        body = json.loads(out)
    except json.JSONDecodeError:
        return []
    rows: list[IngestNode] = []
    for pr in body:
        rows.append(
            IngestNode(
                id=f"gh-pr-{pr['number']}",
                kind="pr",
                title=pr.get("title") or "",
                body=pr.get("body") or "",
                source="github",
                url=pr.get("url") or "",
                state=pr.get("state") or "",
                metadata={
                    "number": pr.get("number"),
                    "labels": [
                        lab.get("name") for lab in (pr.get("labels") or []) if isinstance(lab, dict)
                    ],
                    "author": (pr.get("author") or {}).get("login"),
                    "merged_at": pr.get("mergedAt"),
                },
            )
        )
    return rows


def ingest_github(
    store: Store,
    repo_root: Path,
    *,
    issues: bool = True,
    prs: bool = True,
    state: str = "all",
    limit: int = 200,
) -> dict[str, Any]:
    """End-to-end ``gh``-backed ingest. Requires ``gh`` on PATH and an
    authenticated user (``gh auth status`` should return success).
    """
    summary: dict[str, Any] = {"issues": 0, "prs": 0}
    symbols = store.all_symbols()
    if issues:
        nodes = fetch_github_issues(repo_root, state=state, limit=limit)
        attribute_refs(nodes, symbols)
        store_ingest_nodes(store, nodes, replace_kind="issue")
        summary["issues"] = len(nodes)
    if prs:
        nodes = fetch_github_prs(repo_root, state=state, limit=limit)
        attribute_refs(nodes, symbols)
        store_ingest_nodes(store, nodes, replace_kind="pr")
        summary["prs"] = len(nodes)
    return summary


__all__ = [
    "INGEST_NODE_SCHEMA",
    "IngestNode",
    "attribute_refs",
    "ensure_ingest_table",
    "fetch_github_issues",
    "fetch_github_prs",
    "ingest_adr",
    "ingest_github",
    "load_ingest_nodes",
    "parse_adr",
    "parse_adr_folder",
    "store_ingest_nodes",
]
