"""Layer 26 — multi-agent and orchestration semantics.

* Read snapshots — subagents take a stable read-only handle keyed by
  ``snapshot_id``; other agents can mutate via writeback while the
  reader's view stays consistent. Postgres-style MVCC for the graph.
* Write coordination — annotation writes serialise per-target; conflicts
  surface to ``flag_ambiguous`` rather than failing.
* Agent attribution — every annotation/correction/rule check records
  which agent did it (already in audit_log via ``agent`` field).
* Subagent budgets — caps per-agent token budgets so a runaway
  subagent doesn't drain context.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .indexes import InMemoryGraph


@dataclass
class Snapshot:
    id: str
    graph: InMemoryGraph
    created_at: float
    pinned_by: set[str] = field(default_factory=set)


@dataclass
class AgentBudget:
    agent_id: str
    cap_tokens: int
    used_tokens: int = 0
    blocked: bool = False


class SnapshotRegistry:
    """Process-wide MVCC-style snapshot store. The daemon's mutating
    refresh swaps the *current* snapshot atomically; pinned old
    snapshots stay alive until released.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}
        self._budgets: dict[str, AgentBudget] = {}
        self._write_locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    # ---- read snapshots

    def take(self, graph: InMemoryGraph, *, agent_id: str) -> Snapshot:
        sid = uuid.uuid4().hex[:12]
        with self._global_lock:
            snap = Snapshot(id=sid, graph=graph, created_at=time.time())
            snap.pinned_by.add(agent_id)
            self._snapshots[sid] = snap
        return snap

    def get(self, snapshot_id: str) -> Snapshot | None:
        return self._snapshots.get(snapshot_id)

    def release(self, snapshot_id: str, agent_id: str) -> bool:
        with self._global_lock:
            snap = self._snapshots.get(snapshot_id)
            if snap is None:
                return False
            snap.pinned_by.discard(agent_id)
            if not snap.pinned_by:
                self._snapshots.pop(snapshot_id, None)
            return True

    def list_active(self) -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "created_at": s.created_at,
                "pinned_by": sorted(s.pinned_by),
                "n_symbols": len(s.graph.by_id),
            }
            for s in self._snapshots.values()
        ]

    # ---- write coordination

    def acquire_write_lock(self, target: str) -> threading.Lock:
        """Per-target lock for serialised annotation writes."""
        with self._global_lock:
            lock = self._write_locks.get(target)
            if lock is None:
                lock = threading.Lock()
                self._write_locks[target] = lock
            return lock

    # ---- subagent budgets

    def register_agent(self, agent_id: str, *, cap_tokens: int) -> AgentBudget:
        with self._global_lock:
            budget = AgentBudget(agent_id=agent_id, cap_tokens=cap_tokens)
            self._budgets[agent_id] = budget
            return budget

    def charge(self, agent_id: str, tokens: int) -> AgentBudget:
        b = self._budgets.get(agent_id)
        if b is None:
            return AgentBudget(agent_id=agent_id, cap_tokens=0, blocked=True)
        b.used_tokens += int(tokens)
        if b.used_tokens >= b.cap_tokens:
            b.blocked = True
        return b

    def is_blocked(self, agent_id: str) -> bool:
        b = self._budgets.get(agent_id)
        return bool(b and b.blocked)

    def reset_budget(self, agent_id: str) -> None:
        b = self._budgets.get(agent_id)
        if b is not None:
            b.used_tokens = 0
            b.blocked = False

    def list_budgets(self) -> list[dict[str, Any]]:
        return [b.__dict__ for b in self._budgets.values()]


_REGISTRY: SnapshotRegistry | None = None


def get_registry() -> SnapshotRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = SnapshotRegistry()
    return _REGISTRY


def reset_registry_for_tests() -> None:
    global _REGISTRY
    _REGISTRY = None


__all__ = [
    "AgentBudget",
    "Snapshot",
    "SnapshotRegistry",
    "get_registry",
    "reset_registry_for_tests",
]
