"""Layer 15.5 — hyperscale mode (sharded daemon coordinator).

For Google/Meta/Microsoft-scale monorepos: split the graph by package,
have one daemon per shard, and a coordinator that merges results
across shards.

This module ships the **coordinator** — the shard daemons themselves
are just normal `gp daemon serve` instances pointed at a sub-path.
The coordinator reads ``.graphify_plus/shards.yaml``, dispatches each
incoming op to every shard in parallel, and merges the results.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import DaemonClient

log = logging.getLogger("graphify_plus.daemon.hyperscale")


@dataclass
class Shard:
    name: str
    repo_path: Path
    description: str = ""


@dataclass
class ShardConfig:
    shards: list[Shard] = field(default_factory=list)


def load_shards(root: Path) -> ShardConfig:
    p = root / ".graphify_plus" / "shards.yaml"
    if not p.exists():
        return ShardConfig()
    try:
        import yaml

        body = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("invalid shards.yaml: %s", exc)
        return ShardConfig()
    shards: list[Shard] = []
    for raw in body.get("shards") or []:
        if not isinstance(raw, dict):
            continue
        shards.append(
            Shard(
                name=str(raw.get("name", "?")),
                repo_path=Path(str(raw.get("path", ""))).expanduser(),
                description=str(raw.get("description", "")),
            )
        )
    return ShardConfig(shards=shards)


def fan_out(
    cfg: ShardConfig,
    op: str,
    args: dict[str, Any],
    *,
    timeout_s: float = 5.0,
    max_workers: int = 8,
) -> dict[str, Any]:
    """Call ``op(args)`` against every shard's daemon in parallel.
    Merges ``results`` lists, sums ``count``-style fields, and
    surfaces per-shard error info.
    """
    if not cfg.shards:
        return {"results": [], "shards": []}
    merged_rows: list[dict[str, Any]] = []
    per_shard: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures: dict[concurrent.futures.Future[Any], Shard] = {}
        for shard in cfg.shards:
            client = DaemonClient(shard.repo_path, timeout_s=timeout_s)
            futures[ex.submit(_call_safe, client, op, args)] = shard
        for fut in concurrent.futures.as_completed(futures):
            shard = futures[fut]
            try:
                resp = fut.result()
            except Exception as exc:  # noqa: BLE001
                per_shard.append({"shard": shard.name, "error": str(exc)})
                continue
            if not resp.get("ok"):
                per_shard.append({"shard": shard.name, "error": resp.get("error")})
                continue
            results = resp.get("results") or []
            for row in results:
                row = dict(row)
                row["shard"] = shard.name
                merged_rows.append(row)
            per_shard.append(
                {
                    "shard": shard.name,
                    "count": len(results),
                    "more_available": resp.get("more_available", 0),
                }
            )
    return {"results": merged_rows, "shards": per_shard}


def _call_safe(client: DaemonClient, op: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return client.call(op, args)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": {"code": "SHARD_FAILED", "message": str(exc)}}


__all__ = [
    "Shard",
    "ShardConfig",
    "fan_out",
    "load_shards",
]
