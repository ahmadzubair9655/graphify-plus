"""Layer 27.4 — auto-changelog generation from git history.

`gp daemon changelog --since vX.Y.Z` walks ``git log`` between the
target ref and HEAD, groups commits by conventional-commit type
(feat / fix / docs / chore / test / refactor / perf), and emits
Markdown ready to paste into CHANGELOG.md.

Filters out merge commits and excludes self-mentions (Co-Authored-By
lines).
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("graphify_plus.daemon.changelog")

CONVENTIONAL_RE = re.compile(
    r"^(?P<type>feat|fix|docs|chore|test|refactor|perf|style|build|ci|revert)"
    r"(?P<scope>\([^)]*\))?:\s*(?P<msg>.+)$",
    re.IGNORECASE,
)


@dataclass
class CommitEntry:
    sha: str
    short_sha: str
    type: str
    scope: str
    message: str
    full_subject: str


@dataclass
class ChangelogReport:
    since_ref: str
    head_ref: str
    by_type: dict[str, list[CommitEntry]]
    other: list[CommitEntry]


def collect_commits(repo: Path, since_ref: str, head_ref: str = "HEAD") -> list[CommitEntry]:
    """Run ``git log since_ref..head_ref --no-merges`` and parse."""
    try:
        out = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "log",
                "--no-merges",
                "--pretty=format:%H %h %s",
                f"{since_ref}..{head_ref}",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except subprocess.CalledProcessError as exc:
        log.warning("git log failed: %s", exc)
        return []
    except FileNotFoundError:
        return []
    rows: list[CommitEntry] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            sha, short_sha, subject = line.split(" ", 2)
        except ValueError:
            continue
        m = CONVENTIONAL_RE.match(subject)
        if m:
            rows.append(
                CommitEntry(
                    sha=sha,
                    short_sha=short_sha,
                    type=m.group("type").lower(),
                    scope=(m.group("scope") or "").strip("()"),
                    message=m.group("msg").strip(),
                    full_subject=subject,
                )
            )
        else:
            rows.append(
                CommitEntry(
                    sha=sha,
                    short_sha=short_sha,
                    type="other",
                    scope="",
                    message=subject,
                    full_subject=subject,
                )
            )
    return rows


def group(rows: list[CommitEntry]) -> ChangelogReport:
    by_type: dict[str, list[CommitEntry]] = defaultdict(list)
    other: list[CommitEntry] = []
    for r in rows:
        if r.type == "other":
            other.append(r)
        else:
            by_type[r.type].append(r)
    return ChangelogReport(since_ref="", head_ref="", by_type=dict(by_type), other=other)


_SECTION_TITLES = {
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Performance",
    "refactor": "Changed",
    "docs": "Docs",
    "test": "Tests",
    "chore": "Chore",
    "build": "Build",
    "ci": "CI",
    "style": "Style",
    "revert": "Reverted",
}


def render(report: ChangelogReport) -> str:
    if not report.by_type and not report.other:
        return f"# Changes since {report.since_ref}\n\n_no commits_"
    lines: list[str] = [f"# Changes since {report.since_ref}", ""]
    for kind in ("feat", "fix", "perf", "refactor", "docs", "test", "chore", "build", "ci"):
        rows = report.by_type.get(kind)
        if not rows:
            continue
        lines.append(f"## {_SECTION_TITLES.get(kind, kind.title())}")
        lines.append("")
        for r in rows:
            scope = f"**{r.scope}**: " if r.scope else ""
            lines.append(f"- {scope}{r.message} (`{r.short_sha}`)")
        lines.append("")
    if report.other:
        lines.append("## Other")
        lines.append("")
        for r in report.other:
            lines.append(f"- {r.message} (`{r.short_sha}`)")
        lines.append("")
    return "\n".join(lines)


def make_changelog(repo: Path, since_ref: str, head_ref: str = "HEAD") -> ChangelogReport:
    rows = collect_commits(repo, since_ref, head_ref)
    rep = group(rows)
    rep.since_ref = since_ref
    rep.head_ref = head_ref
    return rep


__all__ = [
    "ChangelogReport",
    "CommitEntry",
    "CONVENTIONAL_RE",
    "collect_commits",
    "group",
    "make_changelog",
    "render",
]
