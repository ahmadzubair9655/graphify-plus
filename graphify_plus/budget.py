"""Backward-compat shim: top-level ``graphify_plus.budget`` re-exports
``graphify_plus.query.budget``. Prefer the new path in fresh code.
"""

from __future__ import annotations

from .query.budget import BudgetedContext, count_tokens, frame_to_budget  # noqa: F401

__all__ = ["BudgetedContext", "count_tokens", "frame_to_budget"]
