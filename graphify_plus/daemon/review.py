"""PR review co-pilot — Sprint 10.1 of the master plan.

Composes everything the rest of the daemon already does into a single
report that's worth posting on a PR:

  * **Architectural rules**: violations introduced by the diff (Layer 9.4).
  * **Untested touched**: changed nodes with low / zero coverage (Layer 7.1).
  * **Blast radius**: dependents of changed nodes — what else might break.
  * **Central touched**: top-50 PageRank nodes that the diff edits — these
    are the changes a reviewer should look at most carefully.
  * **Structural summary**: a one-paragraph natural-language description
    of what the diff did at the symbol level.

The command takes a base/head pair (default: ``HEAD`` vs the merge-base of
``main``) and produces a Markdown report. It deliberately does **not**
post anywhere — that's the GitHub Action's job. ``gp daemon review``
prints to stdout so CI templates can pipe it into ``gh pr comment``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .indexes import InMemoryGraph

log = logging.getLogger("graphify_plus.daemon.review")

DIFF_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")


# ---- diff parsing --------------------------------------------------------


@dataclass
class FileChange:
    path: str
    added_lines: list[int] = field(default_factory=list)
    removed: bool = False
    added: bool = False


def changes_from_unified_diff(text: str) -> list[FileChange]:
    """Parse a unified diff into per-file changed-line lists.

    We only track *added* lines (file:newline). For "what does the reviewer
    need to understand?" purposes that's the right granularity — the
    pre-image is, by definition, the previous state.
    """
    changes: list[FileChange] = []
    cur: FileChange | None = None
    cur_line = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            if cur is not None:
                changes.append(cur)
            cur = None
            cur_line = 0
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                # File deleted — finalise the existing FileChange below if any.
                if cur is not None:
                    cur.removed = True
                continue
            # Drop the "b/" prefix from git's diff format.
            path = target[2:] if target.startswith("b/") else target
            cur = FileChange(path=path)
            cur_line = 0
            continue
        if cur is None:
            continue
        m = DIFF_HUNK_RE.match(raw)
        if m:
            cur_line = int(m.group(1)) - 1
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            cur_line += 1
            cur.added_lines.append(cur_line)
        elif raw.startswith("-") and not raw.startswith("---"):
            # deletion in pre-image; new-side counter doesn't move
            pass
        elif raw.startswith(" "):
            cur_line += 1
    if cur is not None:
        changes.append(cur)
    return changes


# ---- review object -------------------------------------------------------


@dataclass
class TouchedNode:
    node_id: str
    label: str
    source_file: str
    line_number: int
    kind: str
    is_central: bool = False
    coverage_pct: float | None = None
    n_dependents: int = 0
    new: bool = False  # symbol added by this diff (best-effort: line in span ∩ added_lines)


@dataclass
class Review:
    base: str
    head: str
    files_changed: int = 0
    files_renamed: int = 0
    touched: list[TouchedNode] = field(default_factory=list)
    untested_touched: list[TouchedNode] = field(default_factory=list)
    central_touched: list[TouchedNode] = field(default_factory=list)
    rules_violations: list[dict[str, Any]] = field(default_factory=list)
    rules_grade: str = "A"
    blast_radius: list[TouchedNode] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "head": self.head,
            "files_changed": self.files_changed,
            "files_renamed": self.files_renamed,
            "touched": [n.__dict__ for n in self.touched],
            "untested_touched": [n.__dict__ for n in self.untested_touched],
            "central_touched": [n.__dict__ for n in self.central_touched],
            "blast_radius": [n.__dict__ for n in self.blast_radius],
            "rules_violations": list(self.rules_violations),
            "rules_grade": self.rules_grade,
            "summary": self.summary,
        }


# ---- review pipeline -----------------------------------------------------


def make_review(
    graph: InMemoryGraph,
    *,
    base: str = "main",
    head: str = "HEAD",
    diff_text: str | None = None,
) -> Review:
    """Build a review for ``base..head``.

    If ``diff_text`` is provided, parse that directly (test-friendly).
    Otherwise, shell out to git to compute the diff.
    """
    review = Review(base=base, head=head)
    if diff_text is None:
        diff_text = _git_diff(graph.repo_root, base, head)
    if diff_text is None:
        review.summary = "Could not compute diff (is this a git repo? does the base ref exist?)"
        return review

    changes = changes_from_unified_diff(diff_text)
    review.files_changed = len(changes)
    review.files_renamed = sum(1 for c in changes if c.removed)

    touched_ids: set[str] = set()
    new_ids: set[str] = set()
    for change in changes:
        sym_ids = graph.by_path.get(change.path) or []
        for sid in sym_ids:
            sym = graph.by_id[sid]
            span = sym.get("span") or (0, 0)
            start, end = int(span[0]), int(span[1])
            if any(start <= ln <= end for ln in change.added_lines):
                touched_ids.add(sid)
                # Heuristic: if every line in the symbol's span is in
                # added_lines, treat as new.
                if all((start <= ln <= end) for ln in change.added_lines if start <= ln <= end):
                    if change.added_lines and len(set(change.added_lines)) >= max(
                        1, end - start - 1
                    ):
                        new_ids.add(sid)

    pr_set = {sid for sid, _ in graph.pagerank_top}
    for sid in touched_ids:
        sym = graph.by_id[sid]
        cov = graph.coverage.get(sid)
        node = TouchedNode(
            node_id=sid,
            label=sym.get("qualified_name") or sym.get("name") or sid,
            source_file=sym.get("path") or "",
            line_number=int((sym.get("span") or (0, 0))[0]),
            kind=sym.get("kind") or "",
            is_central=sid in pr_set,
            coverage_pct=(round(cov.get("pct", 0.0) * 100, 1) if cov else None),
            n_dependents=len(graph.in_neighbours.get(sid, [])),
            new=sid in new_ids,
        )
        review.touched.append(node)
        if node.is_central:
            review.central_touched.append(node)
        if node.coverage_pct is not None and node.coverage_pct <= 10.0:
            review.untested_touched.append(node)

    review.touched.sort(key=lambda n: (-n.n_dependents, n.source_file, n.line_number))
    review.central_touched.sort(key=lambda n: -n.n_dependents)
    review.untested_touched.sort(key=lambda n: (n.coverage_pct or 0.0, n.source_file))

    # Blast radius — direct dependents of touched nodes that are NOT
    # themselves touched. Capped to avoid overwhelming reports.
    seen_radius: set[str] = set()
    for node in review.touched:
        for src, _kind, _span in graph.in_neighbours.get(node.node_id, []):
            if src in touched_ids or src in seen_radius:
                continue
            sym = graph.by_id.get(src)
            if not sym:
                continue
            seen_radius.add(src)
            review.blast_radius.append(
                TouchedNode(
                    node_id=src,
                    label=sym.get("qualified_name") or sym.get("name") or src,
                    source_file=sym.get("path") or "",
                    line_number=int((sym.get("span") or (0, 0))[0]),
                    kind=sym.get("kind") or "",
                    is_central=src in pr_set,
                    coverage_pct=(
                        round(graph.coverage[src].get("pct", 0.0) * 100, 1)
                        if src in graph.coverage
                        else None
                    ),
                    n_dependents=len(graph.in_neighbours.get(src, [])),
                )
            )
            if len(review.blast_radius) >= 24:
                break
        if len(review.blast_radius) >= 24:
            break

    # Architectural rules — reuse the existing handler, surface violations.
    try:
        from .handlers import rules_check as _rules_check

        rc = _rules_check(graph, {})
        review.rules_violations = rc.get("results", [])
        review.rules_grade = rc.get("extra", {}).get("grade", "A")
    except Exception as exc:  # noqa: BLE001
        log.debug("rules_check failed during review: %s", exc)

    review.summary = _structural_summary(review)
    return review


def _git_diff(repo_root: Path, base: str, head: str) -> str | None:
    """Return the unified diff between two refs, or None on failure.

    Uses GitPython, already a hard dep. Falls back to plain ``git diff``
    via subprocess if GitPython errors (e.g. shallow clones or unusual
    setups).
    """
    try:
        from git import Repo  # type: ignore[import-untyped]

        r = Repo(str(repo_root))
        merge_base = r.merge_base(base, head)
        if merge_base:
            base_sha = merge_base[0].hexsha
        else:
            base_sha = base
        return r.git.diff(base_sha, head, "--no-color", unified=3)
    except Exception as exc:  # noqa: BLE001
        log.debug("GitPython diff failed: %s — falling back to subprocess", exc)
        import subprocess

        try:
            return subprocess.check_output(
                ["git", "-C", str(repo_root), "diff", "--no-color", f"{base}...{head}"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except Exception as exc2:  # noqa: BLE001
            log.debug("subprocess git diff also failed: %s", exc2)
            return None


def _structural_summary(review: Review) -> str:
    if not review.touched:
        return "No structural changes detected — diff appears to be docs / tests / config only."
    n = len(review.touched)
    files = len({n.source_file for n in review.touched if n.source_file})
    parts: list[str] = []
    parts.append(f"Changes touch {n} symbol(s) across {files} file(s).")
    if review.central_touched:
        names = ", ".join(n.label for n in review.central_touched[:3])
        parts.append(
            f"Includes {len(review.central_touched)} central node(s) "
            f"(top-50 PageRank): {names}{'…' if len(review.central_touched) > 3 else ''}."
        )
    if review.untested_touched:
        parts.append(
            f"{len(review.untested_touched)} touched node(s) have ≤10% test coverage — "
            "add tests before merge."
        )
    if review.rules_violations:
        n_err = sum(1 for v in review.rules_violations if v.get("severity") == "error")
        if n_err:
            parts.append(f"{n_err} error-severity rule violation(s) — block before merge.")
        else:
            parts.append(f"{len(review.rules_violations)} architectural-rule warning(s).")
    if review.blast_radius:
        parts.append(f"Blast radius: {len(review.blast_radius)} dependent(s) of touched nodes.")
    return " ".join(parts)


def format_review(review: Review) -> str:
    """Render the review as Markdown — what `gp daemon review` prints
    and what a CI template would post to a PR.
    """
    lines: list[str] = []
    lines.append(f"# Review — {review.base}…{review.head}")
    lines.append("")
    lines.append(review.summary)
    lines.append("")
    lines.append(
        f"**Files changed**: {review.files_changed}  •  "
        f"**Symbols touched**: {len(review.touched)}  •  "
        f"**Architectural grade**: {review.rules_grade}"
    )
    lines.append("")
    if review.central_touched:
        lines.append("## ⚠️ Central nodes touched")
        lines.append("")
        for n in review.central_touched:
            lines.append(
                f"- **{n.label}** [{n.kind}] `{n.source_file}:{n.line_number}` "
                f"({n.n_dependents} dependents)"
            )
        lines.append("")
    if review.untested_touched:
        lines.append("## 🧪 Untested code touched")
        lines.append("")
        for n in review.untested_touched:
            pct = f"{n.coverage_pct}%" if n.coverage_pct is not None else "no signal"
            lines.append(
                f"- {n.label} [{n.kind}] `{n.source_file}:{n.line_number}` — coverage **{pct}**"
            )
        lines.append("")
    if review.rules_violations:
        lines.append("## 🚨 Architectural rule violations")
        lines.append("")
        for v in review.rules_violations:
            sev = v.get("severity", "?")
            rid = v.get("rule_id", "?")
            msg = v.get("message", "")
            lines.append(f"- [{sev}] **{rid}**: {msg}")
        lines.append("")
    if review.touched:
        lines.append("## Symbols touched")
        lines.append("")
        for n in review.touched[:20]:
            tag = "**new**" if n.new else f"{n.n_dependents} deps"
            lines.append(f"- {n.label} [{n.kind}] `{n.source_file}:{n.line_number}` ({tag})")
        if len(review.touched) > 20:
            lines.append(f"- … and {len(review.touched) - 20} more")
        lines.append("")
    if review.blast_radius:
        lines.append("## Blast radius (direct dependents not in this diff)")
        lines.append("")
        for n in review.blast_radius:
            lines.append(f"- {n.label} [{n.kind}] `{n.source_file}:{n.line_number}`")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "FileChange",
    "Review",
    "TouchedNode",
    "changes_from_unified_diff",
    "format_review",
    "make_review",
]
