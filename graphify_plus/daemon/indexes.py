"""In-memory graph + pre-computed indexes held by the daemon.

The whole point: the cold path (load symbols from SQLite, build NetworkX
MultiDiGraph, compute communities, build BM25) runs once at daemon
startup. Every subsequent query reads pre-computed structures.

Indexes
-------

* ``by_id``                — symbol_id → Symbol dict (constant time lookup)
* ``by_path``              — POSIX path → ordered list of symbol_ids
* ``by_name``              — lowercase name → list of symbol_ids
* ``by_qname``             — lowercase qualified_name → symbol_id
* ``label_trie``           — sorted list for prefix matching (no extra deps)
* ``out_neighbours``       — src → list[(dst, kind, span)]
* ``in_neighbours``        — dst → list[(src, kind, span)]
* ``inverted_text``        — token → set[symbol_id] (qualified-name + signature + docstring)
* ``pagerank_top``         — list of (symbol_id, score), sorted desc, capped
* ``communities``          — symbol_id → community_index
* ``file_mtimes``          — POSIX path → mtime when graph was built

The ``InMemoryGraph`` is *immutable from the readers' perspective*. To
update it (e.g. after a watcher tick), build a new ``InMemoryGraph`` and
swap the reference under a lock. This keeps reads lock-free.
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx

from ..core.adapters import Edge, Symbol
from ..core.symbol_graph import build as build_graph
from ..runtime.store import Store

log = logging.getLogger("graphify_plus.daemon.indexes")

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
PAGERANK_TOP_N = 50
STALE_PATH_CLIP = 16  # don't surface more than this many stale paths in freshness
PAGERANK_MAX_ITER = 100


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "")]


def _safe_doc(s: Symbol) -> str:
    parts = [
        s.get("qualified_name") or "",
        s.get("name") or "",
        s.get("signature") or "",
        s.get("docstring") or "",
        s.get("kind") or "",
        s.get("language") or "",
    ]
    return " ".join(p for p in parts if p)


@dataclass
class IndexBuildStats:
    elapsed_ms: float
    symbols: int
    edges: int
    pagerank_size: int
    inverted_terms: int


@dataclass
class InMemoryGraph:
    """Frozen snapshot of the symbol graph plus all derived indexes."""

    by_id: dict[str, Symbol] = field(default_factory=dict)
    by_path: dict[str, list[str]] = field(default_factory=dict)
    by_name: dict[str, list[str]] = field(default_factory=dict)
    by_qname: dict[str, str] = field(default_factory=dict)
    sorted_qnames: list[tuple[str, str]] = field(default_factory=list)  # (qname_lower, sid)
    out_neighbours: dict[str, list[tuple[str, str, tuple[int, int] | None]]] = field(
        default_factory=dict
    )
    in_neighbours: dict[str, list[tuple[str, str, tuple[int, int] | None]]] = field(
        default_factory=dict
    )
    inverted_text: dict[str, set[str]] = field(default_factory=dict)
    pagerank_top: list[tuple[str, float]] = field(default_factory=list)
    communities: dict[str, int] = field(default_factory=dict)
    file_mtimes: dict[str, float] = field(default_factory=dict)
    # Placeholders are unresolved-edge targets that aren't real symbols
    # (e.g. ``self.login``, ``s.login``). Indexed by lowercase short-name
    # so ``who_calls(AuthService.login)`` can roll up placeholder traffic.
    placeholders_by_short_name: dict[str, list[str]] = field(default_factory=dict)
    # R13 — precomputed (placeholder → callers) join. The original hot
    # path was O(P × C) where P = placeholders sharing a short name and
    # C = callers per placeholder; on a 38k-symbol graph names like
    # ``compute`` produced 1.78s P99 tail latency. The flattened table
    # turns _inbound's placeholder rollup into O(1) lookup + O(R)
    # iteration of *callers only*. Built once per snapshot, used by
    # every who_calls / dependents_of call.
    callers_by_short_name: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # Symbol-level coverage attribution (Sprint 8). Empty when no
    # coverage has been ingested via ``gp daemon coverage ingest``.
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Layer 9.1 — CVE rows reachable from each symbol_id (best-effort
    # qname-prefix attribution, populated from the ``cve`` table).
    cves_by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cve_rows: list[dict[str, Any]] = field(default_factory=list)
    # Layer 9.2 — SAST findings keyed by symbol_id.
    sast_by_symbol: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Layer 6 — external-source ingest nodes (issues, PRs, ADRs).
    ingest_nodes: list[dict[str, Any]] = field(default_factory=list)
    ingest_refs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Layer 8 — cross-stack edges (HTTP boundary, DB schema).
    cross_edges: list[dict[str, Any]] = field(default_factory=list)
    cross_edges_by_src: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cross_edges_by_dst: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    repo_root: Path = field(default_factory=lambda: Path("."))
    built_at: float = 0.0
    freshness_token: str = ""
    stats: IndexBuildStats | None = None

    # ---- builder ---------------------------------------------------------

    @classmethod
    def from_store(cls, store: Store, repo_root: Path) -> InMemoryGraph:
        t0 = time.perf_counter()
        symbols = store.all_symbols()
        edges = store.all_edges()
        G = build_graph(symbols, edges)
        # Read communities from cache if present (semantic.find populates it).
        comm: dict[str, int] = {}
        cached_comm = store.get_meta("communities_v1")
        if cached_comm:
            try:
                import json as _json

                comm = _json.loads(cached_comm)
            except Exception:  # noqa: BLE001
                comm = {}
        snap = cls._build_indexes(symbols, edges, G, comm, repo_root)
        # Coverage overlay — best-effort: missing table is not an error.
        try:
            from .coverage import load_coverage

            snap.coverage = load_coverage(store)
        except Exception as exc:  # noqa: BLE001
            log.debug("coverage overlay unavailable: %s", exc)
            snap.coverage = {}
        # CVE / SAST overlays — same opt-in pattern.
        try:
            from .overlays import cve_reach_map, load_cve, load_sast

            snap.cve_rows = load_cve(store)
            if snap.cve_rows:
                snap.cves_by_symbol = cve_reach_map(snap.cve_rows, symbols)
            snap.sast_by_symbol = load_sast(store)
        except Exception as exc:  # noqa: BLE001
            log.debug("security overlays unavailable: %s", exc)
        # External-source ingest (issues, PRs, ADRs).
        try:
            from .ingestors import load_ingest_nodes

            snap.ingest_nodes = load_ingest_nodes(store)
            for node in snap.ingest_nodes:
                for sid in node.get("refs", []):
                    snap.ingest_refs.setdefault(sid, []).append(node)
        except Exception as exc:  # noqa: BLE001
            log.debug("ingest overlay unavailable: %s", exc)
        # Cross-stack edges (HTTP, DB).
        try:
            from .cross_stack import load_cross_edges

            snap.cross_edges = load_cross_edges(store)
            for edge in snap.cross_edges:
                snap.cross_edges_by_src.setdefault(edge["src"], []).append(edge)
                snap.cross_edges_by_dst.setdefault(edge["dst"], []).append(edge)
        except Exception as exc:  # noqa: BLE001
            log.debug("cross-stack edges unavailable: %s", exc)
        snap.stats = IndexBuildStats(
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            symbols=len(symbols),
            edges=len(edges),
            pagerank_size=len(snap.pagerank_top),
            inverted_terms=len(snap.inverted_text),
        )
        return snap

    @classmethod
    def _build_indexes(
        cls,
        symbols: list[Symbol],
        edges: list[Edge],
        G: nx.MultiDiGraph,
        communities: dict[str, int],
        repo_root: Path,
    ) -> InMemoryGraph:
        snap = cls(repo_root=repo_root, built_at=time.time())
        snap.communities = dict(communities)

        # Symbol-level indexes
        for s in symbols:
            sid = s["id"]
            snap.by_id[sid] = s
            path = s.get("path") or ""
            if path:
                snap.by_path.setdefault(path, []).append(sid)
            name = (s.get("name") or "").lower()
            if name:
                snap.by_name.setdefault(name, []).append(sid)
            qname = s.get("qualified_name") or ""
            if qname:
                snap.by_qname[qname.lower()] = sid
                snap.sorted_qnames.append((qname.lower(), sid))
            # Inverted-text index
            for tok in _tokenize(_safe_doc(s)):
                snap.inverted_text.setdefault(tok, set()).add(sid)

        snap.sorted_qnames.sort()
        for _path, ids in snap.by_path.items():
            ids.sort(key=lambda sid: (snap.by_id[sid].get("span") or (0, 0))[0])

        # Edge-level adjacency caches + placeholder index. The placeholder
        # index buckets unresolved edge targets by short name (suffix after
        # the last dot) so we can map ``self.login`` / ``s.login`` back to
        # an ``AuthService.login`` query without a linear scan.
        for e in edges:
            src = e.get("src")
            dst = e.get("dst")
            if not src or not dst:
                continue
            kind = e.get("kind") or ""
            span = e.get("span")
            snap.out_neighbours.setdefault(src, []).append((dst, kind, span))
            snap.in_neighbours.setdefault(dst, []).append((src, kind, span))
            if dst not in snap.by_id:
                short = dst.lower().rsplit(".", 1)[-1]
                if short:
                    snap.placeholders_by_short_name.setdefault(short, []).append(dst)
                    # R13: precompute the flattened (short_name → callers)
                    # join so _inbound's placeholder rollup is O(R) at
                    # query time instead of O(P × C). Cost: O(edges)
                    # build-time, paid once.
                    snap.callers_by_short_name.setdefault(short, []).append((src, kind))

        # PageRank top-N
        try:
            if G.number_of_nodes():
                pr = nx.pagerank(G, max_iter=PAGERANK_MAX_ITER, tol=1e-4)
                ranked = sorted(pr.items(), key=lambda kv: -kv[1])
                # Skip placeholder external nodes — they're noise for "what's central".
                # Placeholders aren't in ``by_id`` (only real symbols are), so absence
                # there is the canonical "this is a placeholder" check.
                snap.pagerank_top = [(sid, score) for sid, score in ranked if sid in snap.by_id][
                    :PAGERANK_TOP_N
                ]
        except Exception as exc:  # noqa: BLE001 — pagerank may fail on degenerate graphs
            log.debug("pagerank failed: %s", exc)
            snap.pagerank_top = []

        # File mtimes (used by freshness)
        snap.file_mtimes = cls._snapshot_mtimes(repo_root, snap.by_path.keys())

        # Compact freshness token: first 12 hex of sha256 over (built_at, n_symbols, n_edges)
        h = hashlib.sha256()
        h.update(str(snap.built_at).encode())
        h.update(str(len(symbols)).encode())
        h.update(str(len(edges)).encode())
        snap.freshness_token = h.hexdigest()[:12]
        return snap

    @staticmethod
    def _snapshot_mtimes(repo_root: Path, paths: Iterable[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in paths:
            if not p:
                continue
            full = repo_root / p
            try:
                out[p] = full.stat().st_mtime
            except OSError:
                out[p] = 0.0
        return out

    # ---- freshness queries -----------------------------------------------

    def freshness(self) -> dict[str, Any]:
        """Compute the current freshness envelope by re-stat'ing the
        tracked paths.

        Cheap: one ``stat`` per known source file. Stays well under 5ms
        for the 5k-node target.
        """
        changed: list[str] = []
        for p, mtime in self.file_mtimes.items():
            full = self.repo_root / p
            try:
                cur = full.stat().st_mtime
            except OSError:
                changed.append(p)
                continue
            if cur > mtime + 1e-6:
                changed.append(p)
        n = len(changed)
        if n == 0:
            trust = "FRESH"
            hint = ""
        elif n <= 3:
            trust = "STALE_FILES"
            hint = (
                f"{n} file(s) modified since last rebuild — "
                f"run `gp daemon refresh` for fresh structural answers, "
                f"or fall back to grep on: {', '.join(changed[:3])}"
            )
        else:
            trust = "STALE_FILES"
            hint = (
                f"{n} files modified since last rebuild — "
                f"run `gp daemon refresh` (cheap incremental update planned)."
            )
        built_iso = (
            datetime.fromtimestamp(self.built_at, tz=timezone.utc).isoformat()
            if self.built_at
            else ""
        )
        return {
            "graph_built_at": built_iso,
            "files_changed_since": n,
            "stale_paths": changed[:STALE_PATH_CLIP],
            "trust": trust,
            "freshness_token": self.freshness_token,
            "hint": hint,
        }

    # ---- helpers used by handlers ----------------------------------------

    def symbols_in_path(self, path: str) -> list[Symbol]:
        """Symbols defined in ``path`` (POSIX), ordered by line number."""
        ids = self.by_path.get(path, [])
        return [self.by_id[s] for s in ids]

    def find_by_name(self, label: str, *, fuzzy: bool = True, limit: int = 50) -> list[Symbol]:
        """Exact-then-prefix-then-suffix-then-substring match on name + qualified_name.

        The four-tier match preserves confidence ordering for callers:
            1. exact qualified_name        → confidence 1.0
            2. exact short name            → confidence ~0.95
            3. prefix on qualified_name    → confidence 0.85
            4. ``X.label`` suffix on qname → confidence 0.75
            5. short-name prefix           → confidence 0.7
            6. substring on qualified_name → confidence 0.6

        ``find_by_name("auth")`` therefore returns ``AuthService`` (short-name
        prefix) even when no qname starts with "auth".
        """
        ql = label.lower()
        out: list[Symbol] = []
        seen: set[str] = set()

        def _add(sid: str) -> str:
            """Returns 'full' when the limit is reached, 'ok' otherwise.
            Skipping a duplicate or missing symbol is *not* a stop condition.
            """
            if sid in seen:
                return "ok"
            sym = self.by_id.get(sid)
            if sym is None:
                return "ok"
            seen.add(sid)
            out.append(sym)
            return "full" if len(out) >= limit else "ok"

        sid = self.by_qname.get(ql)
        if sid and _add(sid) == "full":
            return out
        if ql in self.by_name:
            for s in self.by_name[ql][:limit]:
                if _add(s) == "full":
                    return out
        if not fuzzy:
            return out

        # prefix on sorted_qnames (binary search)
        pos = bisect.bisect_left(self.sorted_qnames, (ql, ""))
        while pos < len(self.sorted_qnames) and self.sorted_qnames[pos][0].startswith(ql):
            if _add(self.sorted_qnames[pos][1]) == "full":
                return out
            pos += 1
        # suffix-on-qname (e.g. "Invoice.total" finds "module.Invoice.total")
        suffix_key = "." + ql
        for qname, sid in self.sorted_qnames:
            if qname.endswith(suffix_key) and _add(sid) == "full":
                return out
        # short-name prefix (so "auth" finds "AuthService")
        for name, ids in self.by_name.items():
            if not name.startswith(ql):
                continue
            for s in ids:
                if _add(s) == "full":
                    return out
        # substring on qualified_name — last-resort fuzzy match
        for qname, sid in self.sorted_qnames:
            if ql in qname and _add(sid) == "full":
                return out
        return out

    def text_search(self, query: str, *, limit: int = 50) -> list[tuple[Symbol, float]]:
        """Cheap inverted-index lookup over qname/signature/docstring.

        This is the ``find_by_concept`` fallback when full BM25 + community
        weighting isn't worth the cost (the indexes are pre-computed but
        running it for every keystroke would be wasteful for trivial queries).
        Returns ``(symbol, score)`` where score is the count of matched
        tokens, weighted by inverse document frequency.
        """
        toks = set(_tokenize(query))
        if not toks:
            return []
        scores: dict[str, float] = {}
        from math import log

        n_docs = max(len(self.by_id), 1)
        for tok in toks:
            ids = self.inverted_text.get(tok, set())
            if not ids:
                continue
            idf = log(1.0 + n_docs / (1.0 + len(ids)))
            for sid in ids:
                scores[sid] = scores.get(sid, 0.0) + idf
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        return [(self.by_id[sid], score) for sid, score in ranked if sid in self.by_id]

    def callers_of(self, sid: str) -> list[Symbol]:
        return self._inbound(sid, kinds={"calls", "references"})

    def callees_of(self, sid: str) -> list[Symbol]:
        out: list[Symbol] = []
        seen: set[str] = set()
        for dst, kind, _span in self.out_neighbours.get(sid, []):
            if kind not in ("calls", "references"):
                continue
            sym = self.by_id.get(dst)
            if sym and dst not in seen:
                seen.add(dst)
                out.append(sym)
        return out

    def dependents_of(self, sid: str) -> list[Symbol]:
        """Symbols that depend on ``sid`` (anyone calling, importing,
        extending, implementing, or referencing it).
        """
        return self._inbound(sid, kinds=None)

    def _inbound(self, sid: str, *, kinds: set[str] | None) -> list[Symbol]:
        """Inbound-edge dependents.

        Also follows *placeholder* aliases — when the python adapter (and
        others) emit unresolved targets like ``self.login`` or ``s.login``
        for an instance method call, those land as separate placeholder
        nodes. We map them back to the real symbol by short-name match so
        ``who_calls(AuthService.login)`` returns the real callers.
        """
        out: list[Symbol] = []
        seen: set[str] = set()
        for src, kind, _span in self.in_neighbours.get(sid, []):
            if kinds is not None and kind not in kinds:
                continue
            if src in seen:
                continue
            seen.add(src)
            sym = self.by_id.get(src)
            if sym:
                out.append(sym)

        # Roll in inbound edges from *placeholder* aliases. R13: use the
        # precomputed flattened join (callers_by_short_name) instead of
        # walking placeholders_by_short_name + in_neighbours nested.
        # Same correctness, ~100× faster on 38k-symbol P99.
        target = self.by_id.get(sid)
        if target is None:
            return out
        short = (target.get("name") or "").lower()
        if not short:
            return out
        for src, kind in self.callers_by_short_name.get(short, []):
            if kinds is not None and kind not in kinds:
                continue
            if src in seen or src == sid:
                continue
            seen.add(src)
            sym = self.by_id.get(src)
            if sym:
                out.append(sym)
        return out

    def dependencies_of(self, sid: str) -> list[Symbol]:
        out: list[Symbol] = []
        seen: set[str] = set()
        for dst, _kind, _span in self.out_neighbours.get(sid, []):
            if dst in seen:
                continue
            seen.add(dst)
            sym = self.by_id.get(dst)
            if sym:
                out.append(sym)
        return out


__all__ = ["IndexBuildStats", "InMemoryGraph"]
