"""``gp daemon diagnose`` — operational visibility (Layer 12.4).

One command, one screen, every signal an operator needs to answer "is
graphify-plus broken?":

  * Daemon liveness (running / pid / socket path / uptime)
  * Cache state (path, size, schema_version, last_modified)
  * Snapshot stats (symbols, files, edges, build_ms, freshness_token)
  * Coverage state (ingested? overall %?)
  * Rules state (configured? grade?)
  * Recent telemetry (last hour: calls, p50, p95, error rate)
  * RAM use (process RSS, when psutil is available — graceful degrade)

The output is a Markdown-friendly multi-section report. ``--json`` emits
the same data as a single JSON object for tools / dashboards.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..runtime.store import Store, cache_path
from .client import DaemonClient
from .protocol import pid_path, socket_path

log = logging.getLogger("graphify_plus.daemon.diagnose")


def diagnose(repo_root: Path) -> dict[str, Any]:
    """Gather every operationally-useful signal for ``repo_root``.

    Never raises — each section is best-effort and falls back to a
    short error string when the underlying data isn't available. The
    point is "give me an answer in any state", including
    daemon-down / no-cache / no-coverage scenarios.
    """
    out: dict[str, Any] = {
        "repo": str(repo_root),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    out["daemon"] = _daemon_section(repo_root)
    out["cache"] = _cache_section(repo_root)
    out["snapshot"] = _snapshot_section(repo_root)
    out["coverage"] = _coverage_section(repo_root)
    out["rules"] = _rules_section(repo_root)
    out["telemetry"] = _telemetry_section(repo_root)
    out["process"] = _process_section()
    return out


def _daemon_section(repo: Path) -> dict[str, Any]:
    sock = socket_path(repo)
    pid_file = pid_path(repo)
    pid: int | None = None
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pid = None
    client = DaemonClient(repo, timeout_s=0.5)
    running = client.is_running()
    uptime_s: float | None = None
    if running and pid_file.exists():
        try:
            uptime_s = max(0.0, _now() - pid_file.stat().st_mtime)
        except OSError:
            uptime_s = None
    return {
        "running": running,
        "pid": pid,
        "socket_path": str(sock),
        "pid_path": str(pid_file),
        "uptime_seconds": round(uptime_s, 1) if uptime_s is not None else None,
    }


def _cache_section(repo: Path) -> dict[str, Any]:
    path = cache_path(repo)
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "hint": f"run `gp init --repo {repo}` to create the cache",
        }
    try:
        st = path.stat()
        size_mb = st.st_size / (1024 * 1024)
        last_mod = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        size_mb = 0.0
        last_mod = "?"
    schema = "?"
    try:
        store = Store(path, integrity_check=False)
        try:
            schema = store.get_meta("schema_version") or "?"
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001
        return {
            "exists": True,
            "path": str(path),
            "size_mb": round(size_mb, 2),
            "last_modified": last_mod,
            "schema_version": schema,
            "error": str(exc),
        }
    return {
        "exists": True,
        "path": str(path),
        "size_mb": round(size_mb, 2),
        "last_modified": last_mod,
        "schema_version": schema,
    }


def _snapshot_section(repo: Path) -> dict[str, Any]:
    """Snapshot details — read live from the daemon when running, else
    rebuild a short-lived in-memory copy for the diagnostic.
    """
    client = DaemonClient(repo, timeout_s=2.0)
    if client.is_running():
        try:
            resp = client.call("graph_stats")
            extra = resp.get("extra", {}) or {}
            fresh = resp.get("freshness", {}) or {}
            return {
                "source": "daemon",
                "symbols": extra.get("symbols", 0),
                "edges_out": extra.get("edges_out", 0),
                "files": extra.get("files", 0),
                "pagerank_top_n": extra.get("pagerank_top_n", 0),
                "build_elapsed_ms": extra.get("build_elapsed_ms", 0.0),
                "freshness_token": extra.get("freshness_token", ""),
                "trust": fresh.get("trust", "?"),
                "files_changed_since": fresh.get("files_changed_since", 0),
            }
        except Exception as exc:  # noqa: BLE001
            return {"source": "daemon", "error": str(exc)}

    # Cold path — build a one-shot snapshot. Cheap enough for diagnose.
    if not cache_path(repo).exists():
        return {"source": "cache", "error": "no graph cache; run `gp init` first"}
    try:
        from .indexes import InMemoryGraph

        store = Store(cache_path(repo))
        try:
            snap = InMemoryGraph.from_store(store, repo)
        finally:
            store.close()
        fresh = snap.freshness()
        return {
            "source": "cache",
            "symbols": len(snap.by_id),
            "edges_out": sum(len(v) for v in snap.out_neighbours.values()),
            "files": len(snap.by_path),
            "pagerank_top_n": len(snap.pagerank_top),
            "build_elapsed_ms": snap.stats.elapsed_ms if snap.stats else 0.0,
            "freshness_token": snap.freshness_token,
            "trust": fresh.get("trust", "?"),
            "files_changed_since": fresh.get("files_changed_since", 0),
        }
    except Exception as exc:  # noqa: BLE001
        return {"source": "cache", "error": str(exc)}


def _coverage_section(repo: Path) -> dict[str, Any]:
    if not cache_path(repo).exists():
        return {"ingested": False}
    try:
        from .coverage import coverage_summary as _cov_summary

        store = Store(cache_path(repo), integrity_check=False)
        try:
            summary = _cov_summary(store)
        finally:
            store.close()
        if not summary.get("symbols"):
            return {"ingested": False}
        return {
            "ingested": True,
            "overall_pct": round(summary["overall_pct"] * 100, 1),
            "lines_covered": summary["lines_covered"],
            "lines_total": summary["lines_total"],
            "ingested_at": summary.get("ingested_at"),
            "source": summary.get("source"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ingested": False, "error": str(exc)}


def _rules_section(repo: Path) -> dict[str, Any]:
    rules_path = repo / ".graphify_plus" / "rules.yaml"
    if not rules_path.exists():
        return {"configured": False, "path": str(rules_path)}
    try:
        import yaml

        body = yaml.safe_load(rules_path.read_text()) or {}
        rules = body.get("rules") or []
        return {
            "configured": True,
            "path": str(rules_path),
            "rule_count": len(rules),
            "layers": list((body.get("layers") or {}).keys()),
        }
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "path": str(rules_path), "error": str(exc)}


def _telemetry_section(repo: Path) -> dict[str, Any]:
    from .telemetry import aggregate, telemetry_path

    path = telemetry_path(repo)
    if not path.exists():
        return {"events": 0, "path": str(path)}
    summary = aggregate(repo)
    last_hour = _last_hour_summary(path)
    return {
        "events": summary["total_calls"],
        "ok_rate": summary["ok_rate"],
        "fresh_rate": summary["fresh_rate"],
        "path": str(path),
        "last_hour": last_hour,
        "top_ops": _top_ops(summary),
    }


def _last_hour_summary(path: Path) -> dict[str, Any]:
    """Quick scan of the tail for the last hour of events."""
    import json

    cutoff = _now() - 3600
    n = 0
    errors = 0
    by_op: dict[str, int] = defaultdict(int)
    try:
        with path.open(encoding="utf-8") as fp:
            for line in fp:
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = ev.get("ts") or ""
                try:
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    continue
                if when < cutoff:
                    continue
                n += 1
                if not ev.get("ok"):
                    errors += 1
                by_op[ev.get("op") or "?"] += 1
    except OSError:
        return {"events": 0}
    return {
        "events": n,
        "errors": errors,
        "by_op": dict(by_op),
    }


def _top_ops(summary: dict[str, Any]) -> list[dict[str, Any]]:
    by_op = summary.get("by_op") or {}
    rows = sorted(
        by_op.items(), key=lambda kv: -int(kv[1].get("calls", 0))
    )[:5]
    return [
        {"op": op, "calls": info["calls"], "p50_ms": info.get("p50_ms", 0.0)}
        for op, info in rows
    ]


def _process_section() -> dict[str, Any]:
    """Best-effort process introspection. ``psutil`` is *not* a hard
    dep — when missing, fall back to ``resource.getrusage`` on POSIX.
    """
    try:
        import psutil  # type: ignore[import-not-found]

        p = psutil.Process(os.getpid())
        return {
            "rss_mb": round(p.memory_info().rss / (1024 * 1024), 1),
            "cpu_percent": p.cpu_percent(interval=0.0),
            "num_threads": p.num_threads(),
        }
    except ImportError:
        try:
            import resource

            ru = resource.getrusage(resource.RUSAGE_SELF)
            # macOS reports RSS in bytes; Linux in KiB. Normalise to MB by
            # checking platform.
            import platform

            divisor = 1024 * 1024 if platform.system() == "Darwin" else 1024
            return {
                "rss_mb": round(ru.ru_maxrss / divisor, 1),
                "user_cpu_seconds": round(ru.ru_utime, 2),
            }
        except Exception:  # noqa: BLE001
            return {"rss_mb": None}


def _now() -> float:
    import time

    return time.time()


def format_diagnose(report: dict[str, Any]) -> str:
    """Render the diagnose report as plain Markdown for terminal output."""
    lines: list[str] = []
    lines.append(f"# graphify-plus diagnose — {report.get('repo', '?')}")
    lines.append(f"_{report.get('timestamp', '?')}_")
    lines.append("")

    d = report.get("daemon", {})
    status = "🟢 running" if d.get("running") else "🔴 not running"
    lines.append(f"## Daemon  {status}")
    lines.append(f"  socket : {d.get('socket_path', '?')}")
    if d.get("pid"):
        lines.append(f"  pid    : {d['pid']}")
    if d.get("uptime_seconds") is not None:
        lines.append(f"  uptime : {d['uptime_seconds']}s")
    lines.append("")

    c = report.get("cache", {})
    lines.append("## Cache")
    if c.get("exists"):
        lines.append(f"  path   : {c['path']}")
        lines.append(f"  size   : {c.get('size_mb', '?')} MB")
        lines.append(f"  schema : v{c.get('schema_version', '?')}")
        lines.append(f"  last   : {c.get('last_modified', '?')}")
    else:
        lines.append(f"  ❌ {c.get('hint', 'no cache')}")
    lines.append("")

    s = report.get("snapshot", {})
    lines.append(f"## Snapshot  ({s.get('source', '?')})")
    if "error" in s:
        lines.append(f"  ❌ {s['error']}")
    else:
        lines.append(
            f"  symbols={s.get('symbols', 0)}  files={s.get('files', 0)}  "
            f"edges={s.get('edges_out', 0)}"
        )
        lines.append(
            f"  build={s.get('build_elapsed_ms', 0):.1f}ms  "
            f"trust={s.get('trust', '?')}  "
            f"token={s.get('freshness_token', '')}"
        )
    lines.append("")

    cov = report.get("coverage", {})
    lines.append("## Coverage")
    if cov.get("ingested"):
        lines.append(
            f"  overall: {cov.get('overall_pct', 0)}%  "
            f"({cov.get('lines_covered', 0)}/{cov.get('lines_total', 0)} lines, "
            f"source={cov.get('source', '?')})"
        )
    else:
        lines.append("  not ingested — run `gp daemon coverage ingest <report>`")
    lines.append("")

    r = report.get("rules", {})
    lines.append("## Rules")
    if r.get("configured"):
        lines.append(f"  path   : {r['path']}")
        lines.append(f"  rules  : {r.get('rule_count', 0)}")
        if r.get("layers"):
            lines.append(f"  layers : {', '.join(r['layers'])}")
    else:
        lines.append("  not configured")
    lines.append("")

    t = report.get("telemetry", {})
    lines.append("## Telemetry")
    if t.get("events"):
        last = t.get("last_hour", {})
        lines.append(
            f"  total events: {t['events']}  "
            f"(ok_rate={t.get('ok_rate', 0):.0%}, "
            f"fresh_rate={t.get('fresh_rate', 0):.0%})"
        )
        lines.append(
            f"  last hour   : {last.get('events', 0)} events, "
            f"{last.get('errors', 0)} errors"
        )
        if t.get("top_ops"):
            lines.append("  top ops:")
            for row in t["top_ops"]:
                lines.append(
                    f"    {row['op']:<24} calls={row['calls']:>5}  "
                    f"p50={row.get('p50_ms', 0)}ms"
                )
    else:
        lines.append("  no telemetry yet")
    lines.append("")

    p = report.get("process", {})
    if p.get("rss_mb") is not None:
        lines.append(f"## Process")
        lines.append(f"  RSS    : {p['rss_mb']} MB")
        if "cpu_percent" in p:
            lines.append(f"  CPU%   : {p['cpu_percent']}")
        if "num_threads" in p:
            lines.append(f"  threads: {p['num_threads']}")

    return "\n".join(lines)


__all__ = ["diagnose", "format_diagnose"]
