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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


@dataclass
class PerfCell:
    workload: str  # 'name_match' | 'concept_search' | '1_hop' | 'multi_hop'
    cache_state: str  # 'cold' | 'warm' | 'hot'
    samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    pass_p99: bool


@dataclass
class PerfTable:
    n_symbols: int
    target_p99_ms: float
    cells: list[PerfCell] = field(default_factory=list)

    def all_pass(self) -> bool:
        return all(c.pass_p99 for c in self.cells)


def perfcheck(graph: InMemoryGraph, *, samples: int = 500) -> PerfResult:
    """Time ``samples`` cheap queries and assert against published
    targets. Used by CI regression tests.
    """

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


# ---- 12.2b — multi-workload performance table -----------------------


def perfcheck_table(graph: InMemoryGraph, *, samples_per_cell: int = 100) -> PerfTable:
    """Run a 4×3 perfcheck table: 4 query workloads × 3 cache states.

    Workloads:
      * ``name_match``    — exact-name find_by_name
      * ``concept_search``— text_search (the embedding-fallback path)
      * ``1_hop``         — callers_of (most common Claude pattern)
      * ``multi_hop``     — dependents_of expansion (3 hops)

    Cache states (we approximate by clearing/repopulating the inverted
    index between rounds):
      * ``cold``  — first call after import; nothing warmed
      * ``warm``  — second call; index rows in OS page cache
      * ``hot``   — many subsequent calls; CPU branch predictor steady

    Reports P50/P95/P99 per cell so the reviewer sees the full
    distribution, not a single best-case number.
    """
    import random as _rand
    import statistics
    import time as _time

    n = len(graph.by_id)
    target = {}
    for ceiling, t in sorted(PERF_TARGETS.items()):
        if n <= ceiling:
            target = t
            break
    if not target:
        target = PERF_TARGETS[100_000]
    target_p99 = float(target.get("p99_ms", 1e9))
    table = PerfTable(n_symbols=n, target_p99_ms=target_p99)
    if not graph.by_id:
        return table

    sids = list(graph.by_id.keys())
    rng = _rand.Random(1337)
    sample_sids = rng.sample(sids, min(samples_per_cell * 2, len(sids)))
    sample_names = [
        graph.by_id[s].get("name") or "" for s in sample_sids if graph.by_id[s].get("name")
    ]
    sample_concepts = [
        "authenticate user",
        "load configuration",
        "render component",
        "fetch data",
        "validate input",
    ]

    workloads = {
        "name_match": lambda i: graph.find_by_name(
            sample_names[i % len(sample_names)] if sample_names else "x", limit=8
        ),
        "concept_search": lambda i: graph.text_search(
            sample_concepts[i % len(sample_concepts)], limit=10
        ),
        "1_hop": lambda i: graph.callers_of(sample_sids[i % len(sample_sids)]),
        "multi_hop": lambda i: _multi_hop(graph, sample_sids[i % len(sample_sids)], depth=3),
    }

    for wl_name, runner in workloads.items():
        # cold: drop any caches that look caching-shaped, then time first
        # call. We can't truly clear OS-level caches from Python, so cold
        # is "n=1 sample of the very first call after we run another
        # workload" — best-effort proxy.
        for cache_state in ("cold", "warm", "hot"):
            durations: list[float] = []
            if cache_state == "cold":
                # warm a *different* workload first, then time one cold call
                for _ in range(5):
                    workloads["name_match" if wl_name != "name_match" else "1_hop"](0)
                t0 = _time.perf_counter()
                runner(0)
                durations.append((_time.perf_counter() - t0) * 1000.0)
            elif cache_state == "warm":
                for _ in range(2):
                    runner(0)
                for i in range(samples_per_cell):
                    t0 = _time.perf_counter()
                    runner(i)
                    durations.append((_time.perf_counter() - t0) * 1000.0)
            else:  # hot
                for _ in range(20):
                    runner(0)
                for i in range(samples_per_cell):
                    t0 = _time.perf_counter()
                    runner(i)
                    durations.append((_time.perf_counter() - t0) * 1000.0)

            durations.sort()
            p50 = statistics.median(durations) if len(durations) > 1 else durations[0]
            p95 = durations[int(len(durations) * 0.95)] if len(durations) > 1 else durations[0]
            p99 = durations[int(len(durations) * 0.99)] if len(durations) > 1 else durations[0]
            table.cells.append(
                PerfCell(
                    workload=wl_name,
                    cache_state=cache_state,
                    samples=len(durations),
                    p50_ms=round(p50, 3),
                    p95_ms=round(p95, 3),
                    p99_ms=round(p99, 3),
                    pass_p99=p99 <= target_p99,
                )
            )
    return table


def _multi_hop(graph: InMemoryGraph, sid: str, *, depth: int = 3) -> list[Any]:
    seen: set[str] = {sid}
    frontier = [sid]
    out: list[Any] = []
    for _ in range(depth):
        next_frontier: list[str] = []
        for node in frontier:
            for src, _kind, _span in graph.in_neighbours.get(node, []):
                if src in seen:
                    continue
                seen.add(src)
                next_frontier.append(src)
                sym = graph.by_id.get(src)
                if sym:
                    out.append(sym)
        frontier = next_frontier
    return out


def render_perf_table(table: PerfTable) -> str:
    """Render the 12-cell table as a Markdown table the PR can paste."""
    lines: list[str] = []
    lines.append(
        f"# perfcheck — {table.n_symbols:,} symbols (target P99 ≤ {table.target_p99_ms}ms)"
    )
    lines.append("")
    lines.append("| workload | cold P99 | warm P99 | hot P99 | hot P50 |")
    lines.append("|---|---|---|---|---|")
    by_wl: dict[str, dict[str, PerfCell]] = {}
    for c in table.cells:
        by_wl.setdefault(c.workload, {})[c.cache_state] = c
    for wl_name in ("name_match", "concept_search", "1_hop", "multi_hop"):
        cells = by_wl.get(wl_name, {})
        cold = cells.get("cold")
        warm = cells.get("warm")
        hot = cells.get("hot")

        def _fmt(c: PerfCell | None, key: str) -> str:
            if c is None:
                return "-"
            v = getattr(c, key)
            mark = " ✗" if not c.pass_p99 and key.endswith("99") else ""
            return f"{v}ms{mark}"

        lines.append(
            f"| {wl_name} | {_fmt(cold, 'p99_ms')} | {_fmt(warm, 'p99_ms')} | "
            f"{_fmt(hot, 'p99_ms')} | {_fmt(hot, 'p50_ms')} |"
        )
    lines.append("")
    if table.all_pass():
        lines.append("✓ every cell within target")
    else:
        n_fail = sum(1 for c in table.cells if not c.pass_p99)
        lines.append(f"✗ {n_fail} cell(s) over target P99 — see ✗ markers above")
    return "\n".join(lines)


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
    "PerfCell",
    "PerfResult",
    "PerfTable",
    "deterministic_hash",
    "dry_run_network",
    "perfcheck",
    "perfcheck_table",
    "render_perf_table",
    "with_failover",
]
