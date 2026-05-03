"""Content-Addressed Skeleton Store.

Stores canonical skeleton bodies keyed by SHA-256 (truncated to 16 hex
chars). Multiple symbols whose canonicalised skeleton text is identical
share a single CAS entry — common for boilerplate React components,
generated CRUD handlers, etc.

Hash truncation rationale: 16 hex chars = 64 bits. Birthday collision
probability is ~2^-32 at one billion entries — far past practical scale
for any single repo. Phase 11 will add a collision detector for hosted
mode just in case.
"""

from __future__ import annotations

import hashlib

from ..runtime.store import Store


def hash_skeleton(skeleton: str) -> str:
    """Stable 16-hex-char hash of a canonical skeleton string."""
    return hashlib.sha256(skeleton.encode("utf-8")).hexdigest()[:16]


def put(store: Store, skeleton: str) -> str:
    """Store ``skeleton`` and return its hash.

    Idempotent: writing the same skeleton twice is a no-op.
    """
    h = hash_skeleton(skeleton)
    store.put_skeleton(h, skeleton)
    return h


def get(store: Store, h: str) -> str | None:
    """Return the skeleton body for hash ``h``, or None if absent."""
    return store.get_skeleton(h)


__all__ = ["get", "hash_skeleton", "put"]
