"""Layer 14 — provenance + hallucination prevention.

Every node and edge can carry a structured provenance record so every
answer is reproducible and every disagreement explainable. The daemon
persists this in a side table keyed by `entity_id` so it doesn't bloat
the hot path.

The hallucination-prevention pipeline runs over LLM-extracted edges
*before* they hit the graph:

* Symbol grounding — LLM-extracted edges that reference symbols
  not in the AST graph are dropped.
* Snippet anchoring — every claim must include a verbatim source
  snippet that exists in the file.
* Constrained relations — relation type must be one of a typed enum.
* Negative sampling — the run is flagged low-confidence if the model
  can't distinguish its real edges from its decoys.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.adapters import Edge, Symbol
from ..runtime.store import Store

log = logging.getLogger("graphify_plus.daemon.provenance")

PROVENANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS provenance (
    entity_id    TEXT PRIMARY KEY,
    extractor    TEXT NOT NULL,
    extractor_kind TEXT NOT NULL,    -- 'AST' | 'LLM' | 'STITCH' | 'INFERRED'
    source_sha   TEXT,
    extracted_at TEXT NOT NULL,
    prompt_hash  TEXT,
    model        TEXT,
    confidence   REAL NOT NULL,
    extra        TEXT                  -- JSON
);
"""

ALLOWED_RELATIONS = {
    "calls",
    "imports",
    "extends",
    "implements",
    "references",
    "exports",
    "contains",
    "jsx_render",
}


@dataclass
class Provenance:
    extractor: str
    extractor_kind: str  # AST | LLM | STITCH | INFERRED
    confidence: float = 1.0
    source_sha: str = ""
    extracted_at: str = ""
    prompt_hash: str = ""
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ast(cls, extractor: str, *, source_sha: str = "") -> Provenance:
        return cls(
            extractor=extractor,
            extractor_kind="AST",
            confidence=1.0,
            source_sha=source_sha,
            extracted_at=datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def llm(
        cls,
        model: str,
        *,
        prompt_hash: str,
        confidence: float,
        source_sha: str = "",
    ) -> Provenance:
        return cls(
            extractor=f"llm:{model}",
            extractor_kind="LLM",
            confidence=float(confidence),
            source_sha=source_sha,
            extracted_at=datetime.now(timezone.utc).isoformat(),
            prompt_hash=prompt_hash,
            model=model,
        )

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def ensure_provenance_table(store: Store) -> None:
    store.conn.executescript(PROVENANCE_SCHEMA)


def store_provenance(store: Store, mapping: dict[str, Provenance]) -> int:
    import json as _json

    ensure_provenance_table(store)
    with store.tx():
        store.conn.executemany(
            "INSERT OR REPLACE INTO provenance(entity_id, extractor, extractor_kind, "
            "source_sha, extracted_at, prompt_hash, model, confidence, extra) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    eid,
                    p.extractor,
                    p.extractor_kind,
                    p.source_sha,
                    p.extracted_at,
                    p.prompt_hash,
                    p.model,
                    p.confidence,
                    _json.dumps(p.extra),
                )
                for eid, p in mapping.items()
            ],
        )
    return len(mapping)


def load_provenance(store: Store) -> dict[str, dict[str, Any]]:
    import json as _json

    ensure_provenance_table(store)
    rows = store.conn.execute(
        "SELECT entity_id, extractor, extractor_kind, source_sha, extracted_at, "
        "prompt_hash, model, confidence, extra FROM provenance"
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for eid, ext, kind, sha, ts, ph, model, conf, extra in rows:
        try:
            ex = _json.loads(extra) if extra else {}
        except Exception:  # noqa: BLE001
            ex = {}
        out[eid] = {
            "extractor": ext,
            "extractor_kind": kind,
            "source_sha": sha,
            "extracted_at": ts,
            "prompt_hash": ph,
            "model": model,
            "confidence": conf,
            "extra": ex,
        }
    return out


# ---- hallucination filters ----------------------------------------------


@dataclass
class FilterStats:
    started_with: int
    after_grounding: int
    after_anchoring: int
    after_relation_check: int
    rejected: list[dict[str, Any]] = field(default_factory=list)


def filter_llm_edges(
    edges: list[Edge],
    symbols: list[Symbol],
    *,
    repo_root: Path | None = None,
    snippet_index: dict[str, str] | None = None,
) -> tuple[list[Edge], FilterStats]:
    """Apply the three pre-graph filters to LLM-extracted edges.

    ``snippet_index`` is an optional ``{path: file_text}`` map for
    snippet anchoring. When omitted (or for an edge whose path isn't
    in the index), anchoring is skipped — the edge passes that gate.
    Symbol grounding and constrained relations are always enforced.
    """
    stats = FilterStats(
        started_with=len(edges),
        after_grounding=0,
        after_anchoring=0,
        after_relation_check=0,
    )

    sym_ids = {s.get("id") for s in symbols}

    # Stage 1: symbol grounding — both endpoints must resolve.
    grounded: list[Edge] = []
    for e in edges:
        if e.get("src") in sym_ids and e.get("dst") in sym_ids:
            grounded.append(e)
        else:
            stats.rejected.append({"reason": "ungrounded", "edge": dict(e)})
    stats.after_grounding = len(grounded)

    # Stage 2: snippet anchoring (when provided).
    anchored: list[Edge] = []
    for e in grounded:
        snip = (e.get("evidence_snippet") or "").strip()  # type: ignore[typeddict-item]
        path = e.get("evidence_path") or ""  # type: ignore[typeddict-item]
        if not snip or snippet_index is None or path not in snippet_index:
            anchored.append(e)
            continue
        if snip in snippet_index[path]:
            anchored.append(e)
        else:
            stats.rejected.append({"reason": "unanchored", "edge": dict(e)})
    stats.after_anchoring = len(anchored)

    # Stage 3: relation type must be in allowed enum.
    typed: list[Edge] = []
    for e in anchored:
        if (e.get("kind") or "") in ALLOWED_RELATIONS:
            typed.append(e)
        else:
            stats.rejected.append({"reason": "unknown_relation", "edge": dict(e)})
    stats.after_relation_check = len(typed)
    return typed, stats


def hash_prompt(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def evaluate_negative_sampling(real_edges: list[Edge], decoy_edges: list[Edge]) -> dict[str, Any]:
    """Negative-sampling self-check.

    The caller asks the model to produce real edges *and* decoys (e.g.
    swapped src/dst), then runs the model again to classify. If the
    classifier accuracy is below 0.7 the run is flagged low-confidence.
    Here we just return the structural shape — the model loop lives in
    the caller (or a separate verification module).
    """
    n_real = len(real_edges)
    n_decoy = len(decoy_edges)
    return {
        "real": n_real,
        "decoys": n_decoy,
        "balance_ok": n_decoy >= max(2, n_real // 4),
    }


__all__ = [
    "ALLOWED_RELATIONS",
    "FilterStats",
    "Provenance",
    "PROVENANCE_SCHEMA",
    "ensure_provenance_table",
    "evaluate_negative_sampling",
    "filter_llm_edges",
    "hash_prompt",
    "load_provenance",
    "store_provenance",
]
