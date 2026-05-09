"""Layer 14.4 — continuous correctness sampling.

A small fraction of every user's local extractions get a second-pass
verification. Disagreement rates are aggregated locally and (opt-in)
shipped to a central endpoint.

The implementation here is the *bookkeeping*: a sampling decision
function, a verification record dataclass, and a disagreement
aggregator. The actual second-pass model call is the caller's job
(typically the existing ``verification`` module).
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("graphify_plus.daemon.correctness_sampling")

VERIFICATIONS_FILE = "verifications.jsonl"
DEFAULT_SAMPLING_RATE = 0.01  # 1% of edges


def verifications_path(repo: Path) -> Path:
    return repo / ".graphify_plus" / VERIFICATIONS_FILE


@dataclass
class VerificationRecord:
    entity_id: str
    primary_extractor: str
    primary_value: Any
    secondary_extractor: str
    secondary_value: Any
    disagreement: bool
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def should_sample(entity_id: str, *, rate: float = DEFAULT_SAMPLING_RATE) -> bool:
    """Deterministic sampling: hash the entity_id + a daily nonce so
    the same entity is sampled at the same rate every day, but the
    *which* changes day-over-day to avoid bias toward easy ones.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    h = hashlib.sha256(f"{entity_id}::{today}".encode()).hexdigest()
    bucket = int(h[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def record(repo: Path, rec: VerificationRecord) -> None:
    p = verifications_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not rec.ts:
        rec.ts = datetime.now(timezone.utc).isoformat()
    with p.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(rec.to_dict(), separators=(",", ":")) + "\n")


def disagreement_rate(repo: Path) -> dict[str, Any]:
    p = verifications_path(repo)
    if not p.exists():
        return {"samples": 0, "rate": 0.0}
    total = 0
    disagreed = 0
    by_extractor: dict[str, dict[str, int]] = {}
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            if row.get("disagreement"):
                disagreed += 1
            extractor = row.get("primary_extractor", "?")
            stats = by_extractor.setdefault(extractor, {"samples": 0, "disagreed": 0})
            stats["samples"] += 1
            if row.get("disagreement"):
                stats["disagreed"] += 1
    except OSError:
        return {"samples": 0, "rate": 0.0}
    return {
        "samples": total,
        "disagreed": disagreed,
        "rate": (disagreed / total) if total else 0.0,
        "by_extractor": by_extractor,
    }


def maybe_sample_and_record(
    repo: Path,
    *,
    entity_id: str,
    primary: tuple[str, Any],
    secondary_fn,
    rate: float = DEFAULT_SAMPLING_RATE,
) -> VerificationRecord | None:
    """Convenience wrapper: probabilistically sample the entity, run
    ``secondary_fn`` (e.g. a stronger model), and record the
    disagreement.
    """
    if not should_sample(entity_id, rate=rate):
        return None
    try:
        secondary_extractor, secondary_value = secondary_fn(entity_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("secondary verification failed: %s", exc)
        return None
    rec = VerificationRecord(
        entity_id=entity_id,
        primary_extractor=primary[0],
        primary_value=primary[1],
        secondary_extractor=secondary_extractor,
        secondary_value=secondary_value,
        disagreement=primary[1] != secondary_value,
    )
    record(repo, rec)
    return rec


__all__ = [
    "DEFAULT_SAMPLING_RATE",
    "VERIFICATIONS_FILE",
    "VerificationRecord",
    "disagreement_rate",
    "maybe_sample_and_record",
    "record",
    "should_sample",
    "verifications_path",
]
