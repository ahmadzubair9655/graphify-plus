"""Layer 12 — operational rigor (the unglamorous moat).

* 12.1 Privacy / security model — declared explicitly via
  ``gp daemon privacy``. ``dry-run-network`` reports every byte the
  tool *would* send anywhere.
* 12.2 Performance contract — ``gp daemon perfcheck`` runs a
  fixed-budget benchmark and asserts P50/P99 against published
  targets.
* 12.3 Failure modes — ``with_failover()`` wraps any callable so a
  daemon-down / corrupt-cache / watcher-flooded scenario yields a
  degraded but usable answer.
* 12.5 Deterministic builds — ``deterministic_hash(graph)`` produces
  the same digest for identical input regardless of build order.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .indexes import InMemoryGraph

log = logging.getLogger("graphify_plus.daemon.ops_rigor")


# ---- 12.1 privacy declaration ------------------------------------------


PRIVACY_DECLARATION = """\
# graphify-plus privacy model

By default, **nothing leaves the machine**:
- Symbol graph, annotations, conversation memory, audit log: local-only.
- Telemetry: opt-in only via GRAPHIFY_TELEMETRY_URL.
- LLM extraction: gated; uses the IDE's already-configured key.

What does leave the machine when configured:
- PyPI version-check ping (one HTTP HEAD/day; opt out with GP_NO_UPDATE_CHECK=1).
- gh CLI traffic when you run `gp daemon ingest github` (your auth, your tokens).
- HTTP-style telemetry to your own endpoint when GRAPHIFY_TELEMETRY_URL is set.

What never leaves:
- Source code / file contents.
- Symbol names, qualified names, docstrings.
- Annotations, audit log, ADR / Slack / conversation ingest.

`gp daemon privacy --dry-run-network` reports every outbound endpoint
the current configuration would touch. Run before enterprise rollout.
"""


@dataclass
class NetworkProbe:
    purpose: str
    endpoint: str
    enabled: bool
    note: str = ""


def dry_run_network(repo: Path) -> list[NetworkProbe]:
    """Enumerate every outbound endpoint the current configuration
    would touch. Caller can render or assert "no endpoints enabled"
    for air-gapped audits.
    """
    import os

    probes: list[NetworkProbe] = []
    probes.append(
        NetworkProbe(
            purpose="version check",
            endpoint="https://pypi.org/pypi/graphify-plus/json",
            enabled=os.environ.get("GP_NO_UPDATE_CHECK") != "1",
            note="opt-out via GP_NO_UPDATE_CHECK=1",
        )
    )
    probes.append(
        NetworkProbe(
            purpose="telemetry sink",
            endpoint=os.environ.get("GRAPHIFY_TELEMETRY_URL") or "(none)",
            enabled=bool(os.environ.get("GRAPHIFY_TELEMETRY_URL")),
            note="opt-in via env var",
        )
    )
    probes.append(
        NetworkProbe(
            purpose="GitHub ingest",
            endpoint="github.com (via `gh` CLI)",
            enabled=False,
            note="only on `gp daemon ingest github`; uses gh's own auth",
        )
    )
    probes.append(
        NetworkProbe(
            purpose="local LLM",
            endpoint=os.environ.get("OLLAMA_BASE_URL") or "(none)",
            enabled=bool(os.environ.get("OLLAMA_BASE_URL")),
            note="local network only by default",
        )
    )
    return probes


# ---- 12.2 performance contract ----------------------------------------


PERF_TARGETS = {
    5_000: {"p50_ms": 20, "p99_ms": 100, "ram_mb": 300},
    30_000: {"p50_ms": 50, "p99_ms": 250, "ram_mb": 1024},
    100_000: {"p50_ms": 100, "p99_ms": 500, "ram_mb": 3072},
}


@dataclass
class PerfResult:
    n_symbols: int
    samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    target: dict[str, Any] = field(default_factory=dict)
    pass_p50: bool = True
    pass_p99: bool = True


def perfcheck(graph: InMemoryGraph, *, samples: int = 500) -> PerfResult:
    """Time ``samples`` cheap queries and assert against published
    targets. Used by CI regression tests.
    """
    import statistics

    if not graph.by_id:
        return PerfResult(n_symbols=0, samples=0, p50_ms=0.0, p95_ms=0.0, p99_ms=0.0)
    n = len(graph.by_id)
    target = {}
    for ceiling, t in sorted(PERF_TARGETS.items()):
        if n <= ceiling:
            target = t
            break
    if not target:
        target = PERF_TARGETS[100_000]

    durations: list[float] = []
    sids = list(graph.by_id.keys())
    import random as _rand

    rng = _rand.Random(1337)
    for _ in range(samples):
        sid = rng.choice(sids)
        t0 = time.perf_counter()
        graph.callers_of(sid)
        graph.find_by_name(graph.by_id[sid].get("name") or "?", limit=8)
        durations.append((time.perf_counter() - t0) * 1000.0)
    durations.sort()
    p50 = durations[len(durations) // 2]
    p95 = durations[int(len(durations) * 0.95)]
    p99 = durations[int(len(durations) * 0.99)]
    return PerfResult(
        n_symbols=n,
        samples=samples,
        p50_ms=round(p50, 2),
        p95_ms=round(p95, 2),
        p99_ms=round(p99, 2),
        target=target,
        pass_p50=p50 <= target.get("p50_ms", 1e9),
        pass_p99=p99 <= target.get("p99_ms", 1e9),
    )


# ---- 12.3 failure modes ----------------------------------------------


def with_failover(primary: Callable[[], Any], fallback: Callable[[], Any]) -> Any:
    """Run ``primary``; if it raises, call ``fallback`` and tag the
    result with ``degraded: true``. Both callables return dicts.
    """
    try:
        out = primary()
        if isinstance(out, dict):
            out.setdefault("degraded", False)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("primary failed (%s); using fallback", exc)
        out = fallback()
        if isinstance(out, dict):
            out["degraded"] = True
            out["degraded_reason"] = str(exc)
        return out


# ---- 12.5 deterministic build hash ------------------------------------


def deterministic_hash(graph: InMemoryGraph) -> str:
    """SHA-256 over a canonical serialisation of the graph: symbol
    qualified-names + edge tuples, both sorted. Two engineers on the
    same SHA see the same digest.
    """
    h = hashlib.sha256()
    for sid in sorted(graph.by_id):
        sym = graph.by_id[sid]
        h.update(sid.encode())
        h.update(b"|")
        h.update((sym.get("qualified_name") or "").encode())
        h.update(b"|")
        h.update(str(sym.get("kind") or "").encode())
        h.update(b"\n")
    edges: list[tuple[str, str, str]] = []
    for src, neighbours in graph.out_neighbours.items():
        for dst, kind, _span in neighbours:
            edges.append((src, dst, kind or ""))
    for src, dst, kind in sorted(edges):
        h.update(f"{src}->{dst}|{kind}\n".encode())
    return h.hexdigest()[:16]


__all__ = [
    "NetworkProbe",
    "PERF_TARGETS",
    "PRIVACY_DECLARATION",
    "PerfResult",
    "deterministic_hash",
    "dry_run_network",
    "perfcheck",
    "with_failover",
]
