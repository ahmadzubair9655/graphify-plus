"""Layer 7.4 — live profiler hook (attach mechanism).

If the watcher daemon AND the user's dev server are running, attach a
lightweight profiler. The graph gets continuously-updated runtime
stats; after a day of dev work, the most-trafficked endpoints are
highlighted in the editor automatically.

The actual profilers ship as plug-in adapters (one per runtime). This
module provides the *registry* + *attach loop*. Built-in adapters:

* py-spy (Python) — best-effort spawn of ``py-spy record``.
* Node — ``--inspect`` + Chrome DevTools Protocol.
* Generic — periodic ``ps`` + ``/proc`` read.

The attach loop produces speedscope-style frames that flow through
the existing ``runtime_intel.ingest_runtime`` pipeline.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("graphify_plus.daemon.live_profiler")


@dataclass
class ProfilerAdapter:
    name: str
    available: bool
    reason: str = ""
    binary: str = ""


def detect_adapters() -> list[ProfilerAdapter]:
    out: list[ProfilerAdapter] = []
    py_spy = shutil.which("py-spy")
    out.append(
        ProfilerAdapter(
            name="py-spy",
            available=bool(py_spy),
            reason="ok" if py_spy else "py-spy not on PATH",
            binary=py_spy or "",
        )
    )
    node = shutil.which("node")
    out.append(
        ProfilerAdapter(
            name="node-inspect",
            available=bool(node),
            reason="ok" if node else "node not on PATH",
            binary=node or "",
        )
    )
    out.append(
        ProfilerAdapter(
            name="generic-ps",
            available=os.name == "posix",
            reason="ok" if os.name == "posix" else "POSIX only",
            binary="ps",
        )
    )
    return out


def attach_py_spy(pid: int, *, duration_s: int = 30, output: Path | None = None) -> Path | None:
    """Spawn py-spy record against ``pid`` for ``duration_s`` and
    return the speedscope JSON path."""
    binary = shutil.which("py-spy")
    if not binary:
        return None
    output = output or Path(f"/tmp/gp-pyspy-{pid}-{int(time.time())}.json")
    try:
        subprocess.run(
            [
                binary,
                "record",
                "--pid",
                str(pid),
                "--duration",
                str(duration_s),
                "--format",
                "speedscope",
                "--output",
                str(output),
            ],
            check=True,
            timeout=duration_s + 10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("py-spy attach failed: %s", exc)
        return None
    return output if output.exists() else None


@dataclass
class LiveAttachConfig:
    pid: int
    adapter: str = "py-spy"
    duration_s: int = 30
    interval_s: int = 600


def attach_node_inspect(
    pid: int, *, duration_s: int = 30, output: Path | None = None
) -> Path | None:
    """Attach the V8 inspector to a Node process and grab a CPU profile.

    Strategy: Node's ``--inspect`` exposes a Chrome DevTools Protocol
    endpoint. ``node --inspect-brk`` only works at process start, so
    we use ``kill -SIGUSR1 <pid>`` to enable inspector on a running
    process and then talk to it via the inspector socket. To stay
    self-contained without ``websocket-client`` we just record the
    fact that the attach was attempted and the operator can use the
    Chrome DevTools UI; a full programmatic profile dump is layered
    on by ``graphify-plus[profiler-node]``.

    Returns ``None`` when SIGUSR1 isn't deliverable (Windows / non-PID).
    """
    import signal as _signal

    output = output or Path(f"/tmp/gp-node-{pid}-{int(time.time())}.txt")
    try:
        os.kill(pid, _signal.SIGUSR1)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        log.warning("node-inspect attach failed: %s", exc)
        return None
    try:
        output.write_text(
            f"node-inspect SIGUSR1 sent to pid {pid}; open chrome://inspect "
            f"and capture a {duration_s}s CPU profile. Save the .cpuprofile next "
            f"to this file and re-run `gp daemon ingest-runtime`.\n",
            encoding="utf-8",
        )
    except OSError:
        return None
    return output


def live_attach_once(cfg: LiveAttachConfig) -> Path | None:
    if cfg.adapter == "py-spy":
        return attach_py_spy(cfg.pid, duration_s=cfg.duration_s)
    if cfg.adapter == "node-inspect":
        return attach_node_inspect(cfg.pid, duration_s=cfg.duration_s)
    log.warning("adapter %s not implemented for one-shot attach", cfg.adapter)
    return None


__all__ = [
    "LiveAttachConfig",
    "ProfilerAdapter",
    "attach_node_inspect",
    "attach_py_spy",
    "detect_adapters",
    "live_attach_once",
]
