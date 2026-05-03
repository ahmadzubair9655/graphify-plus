# TODO: flip to hard gate once illustrative-only blocks are tagged with doctest:skip
"""11.9 — README doc verification.

Walks ``README.md`` (and any other docs added in future), extracts every
fenced ``bash``/``sh`` block, and runs each block in a subprocess in a
fresh temp directory. A block prefixed by ``<!-- doctest:skip -->`` on
the line immediately above is skipped.

Exit 0 if every executed block returns 0; non-zero otherwise.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md"]

FENCE_RE = re.compile(
    r"(?P<skip><!-- doctest:skip -->\s*\n)?"
    r"```(?P<lang>bash|sh)\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def extract_blocks(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for m in FENCE_RE.finditer(text):
        if m.group("skip"):
            continue
        line = text.count("\n", 0, m.start()) + 1
        out.append((line, m.group("body")))
    return out


def run_block(body: str, cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", body],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    failures = 0
    for doc in DOCS:
        if not doc.exists():
            continue
        text = doc.read_text()
        for line, body in extract_blocks(text):
            with tempfile.TemporaryDirectory() as td:
                rc, _out, err = run_block(body, Path(td))
            if rc != 0:
                failures += 1
                print(
                    f"FAIL {doc.name}:{line} (exit {rc})\n--- block ---\n{body}\n--- stderr ---\n{err}\n"
                )
            else:
                print(f"OK   {doc.name}:{line}")
    if failures:
        print(f"\n{failures} block(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
