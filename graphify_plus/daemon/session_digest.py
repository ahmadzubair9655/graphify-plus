"""Post-session digest — Layer 13.3 of the master plan.

`gp daemon session-digest [--since <git-ref-or-iso-date>]` produces the
end-of-session summary that "reviewers love you, future-you reading the
PR in 6 months loves you more":

  * What you changed (structural diff, not file diff)
  * What rules you bent (severity-grouped)
  * What's now untested that wasn't before
  * Recommended next steps (high-leverage, derived from the digest)

This is deliberately a *composition* of `review`, `coverage_summary`,
`rules_check`, and `whats_untested` — a single-call wrapper that turns
"what just happened?" into a paste-into-PR-description block.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .indexes import InMemoryGraph
from .review import Review, make_review

log = logging.getLogger("graphify_plus.daemon.session_digest")


@dataclass
class SessionDigest:
    repo: str
    since_ref: str
    head_ref: str
    started_at: str = ""
    ended_at: str = ""
    review: Review | None = None
    coverage_overall_pct: float | None = None
    next_steps: list[str] = field(default_factory=list)
    rules_grade: str = "A"
    rules_violations_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "since_ref": self.since_ref,
            "head_ref": self.head_ref,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "review": self.review.to_dict() if self.review else None,
            "coverage_overall_pct": self.coverage_overall_pct,
            "next_steps": list(self.next_steps),
            "rules_grade": self.rules_grade,
            "rules_violations_count": self.rules_violations_count,
        }


def make_digest(
    graph: InMemoryGraph,
    *,
    since: str = "main",
    head: str = "HEAD",
    diff_text: str | None = None,
) -> SessionDigest:
    digest = SessionDigest(
        repo=graph.repo_root.name,
        since_ref=since,
        head_ref=head,
        ended_at=datetime.now(timezone.utc).isoformat(),
    )

    review = make_review(graph, base=since, head=head, diff_text=diff_text)
    digest.review = review
    digest.rules_grade = review.rules_grade
    digest.rules_violations_count = len(review.rules_violations)

    if graph.coverage:
        total_lc = sum(int(c.get("lines_covered", 0)) for c in graph.coverage.values())
        total_lt = sum(int(c.get("lines_total", 0)) for c in graph.coverage.values())
        digest.coverage_overall_pct = round(total_lc / total_lt * 100.0, 1) if total_lt else None

    digest.next_steps = _next_steps(review, digest.coverage_overall_pct)
    return digest


def _next_steps(review: Review, coverage_pct: float | None) -> list[str]:
    steps: list[str] = []
    if review.untested_touched:
        first = review.untested_touched[0]
        steps.append(
            f"Add tests for {first.label} (`{first.source_file}:{first.line_number}`) "
            f"— currently {first.coverage_pct}% covered."
        )
    if review.rules_violations:
        n_err = sum(1 for v in review.rules_violations if v.get("severity") == "error")
        if n_err:
            steps.append(f"Resolve {n_err} error-severity rule violation(s) before merge.")
    if review.central_touched and not review.untested_touched:
        first = review.central_touched[0]
        steps.append(
            f"Run `gp daemon what_depends_on --arg node={first.label}` "
            f"and double-check the {first.n_dependents} dependent(s) still work."
        )
    if coverage_pct is not None and coverage_pct < 60.0:
        steps.append(
            f"Repo coverage is {coverage_pct}% — run "
            "`gp daemon coverage untested` to find high-leverage gaps."
        )
    if not steps:
        steps.append("All clean — ready to push.")
    return steps


def format_digest(digest: SessionDigest) -> str:
    """Render as Markdown — pastes cleanly into a PR description."""
    lines: list[str] = []
    lines.append(f"# Session digest — {digest.repo}")
    lines.append(f"_{digest.since_ref}…{digest.head_ref}  •  ended {digest.ended_at}_")
    lines.append("")

    if digest.review:
        lines.append("## What changed")
        lines.append("")
        lines.append(digest.review.summary)
        lines.append("")
        if digest.review.touched:
            lines.append("**Symbols touched:**")
            for n in digest.review.touched[:10]:
                lines.append(f"- {n.label} [{n.kind}] `{n.source_file}:{n.line_number}`")
            if len(digest.review.touched) > 10:
                lines.append(f"- … and {len(digest.review.touched) - 10} more")
            lines.append("")

    if digest.review and digest.review.rules_violations:
        lines.append("## Rules bent")
        lines.append("")
        lines.append(
            f"Architectural grade: **{digest.rules_grade}**  "
            f"({digest.rules_violations_count} violation(s))"
        )
        for v in digest.review.rules_violations[:5]:
            lines.append(
                f"- [{v.get('severity', '?')}] **{v.get('rule_id', '?')}**: {v.get('message', '')}"
            )
        lines.append("")

    if digest.review and digest.review.untested_touched:
        lines.append("## Now-untested code")
        lines.append("")
        for n in digest.review.untested_touched[:8]:
            pct = f"{n.coverage_pct}%" if n.coverage_pct is not None else "no signal"
            lines.append(f"- {n.label} `{n.source_file}:{n.line_number}` — coverage **{pct}**")
        lines.append("")

    if digest.coverage_overall_pct is not None:
        lines.append(f"**Repo coverage:** {digest.coverage_overall_pct}%")
        lines.append("")

    lines.append("## Recommended next steps")
    lines.append("")
    for step in digest.next_steps:
        lines.append(f"- {step}")

    return "\n".join(lines)


__all__ = ["SessionDigest", "format_digest", "make_digest"]
