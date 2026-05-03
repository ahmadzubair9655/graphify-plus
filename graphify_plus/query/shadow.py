"""Shadow-graph dry runs.

Stage a hypothetical change on a copy-on-write overlay, evaluate the
unified rules engine + a fast structural audit, return a grade. Never
mutates the cache.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..runtime.overlay import GraphOverlay
from ..runtime.rules import RuleSet, Violation, evaluate, grade


@dataclass(frozen=True)
class ShadowResult:
    grade: str
    violations: list[Violation]
    summary: str


def simulate(overlay: GraphOverlay, ruleset: RuleSet) -> ShadowResult:
    """Evaluate the implied graph; return grade + violations."""
    violations = evaluate(overlay, ruleset)
    g = grade(violations)
    errors = sum(1 for v in violations if v.severity == "error")
    warnings = sum(1 for v in violations if v.severity == "warning")
    summary = f"grade={g} errors={errors} warnings={warnings}"
    return ShadowResult(grade=g, violations=violations, summary=summary)


__all__ = ["ShadowResult", "simulate"]
