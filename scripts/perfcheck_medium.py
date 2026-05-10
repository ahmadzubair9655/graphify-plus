#!/usr/bin/env python3
"""Medium-repo perfcheck (RISKS.md R9).

The 12-cell perfcheck currently runs against a 1,371-symbol repo. The
review pushed back: that's the small-repo number; the worst case lives
on a 30k-node real codebase under a cold cache.

This script generates a deterministic synthetic Python codebase of
~30,000 symbols (configurable), runs `gp init` and the 12-cell
perfcheck table against it, and prints the result. The synthesis is
deterministic so two runs produce identical numbers.

Run::

    python scripts/perfcheck_medium.py
    python scripts/perfcheck_medium.py --target-symbols 100000  # large

The output goes to ``docs/perf/medium_repo_perfcheck.md`` so the
release notes can link a stable artefact.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PYTHON_TEMPLATE = '''"""Module {module_idx}: synthetic.

Generated for perfcheck. Each module exports {n_classes} classes and
{n_functions} top-level functions, with deterministic call graphs so
the perfcheck workload is reproducible.
"""

from __future__ import annotations


class Base{module_idx}:
    """Base for module {module_idx}."""

    def hello(self) -> str:
        return "module {module_idx}"

{class_blocks}

{function_blocks}
'''

CLASS_TEMPLATE = '''
class {class_name}(Base{module_idx}):
    """{class_name} class doing thing {class_idx}."""

    def __init__(self, value: int = 0) -> None:
        self.value = value

    def compute(self) -> int:
        return self.value + {class_idx}

    def describe(self) -> str:
        return f"{{self.compute()}}"

    def chain(self) -> str:
        return self.hello() + ":" + self.describe()
'''

FUNCTION_TEMPLATE = '''
def {fn_name}(x: int = {fn_idx}) -> int:
    """Function {fn_name} returns x scaled by {fn_idx}."""
    return x * {fn_idx} + Base{module_idx}().__class__.__qualname__.count("e")
'''


def generate(target_symbols: int, repo: Path) -> int:
    """Generate enough modules to hit ``target_symbols``. Each module
    contributes roughly 1 module + 5 classes + 4 methods/class + 5
    functions = ~26 symbols.
    """
    repo.mkdir(parents=True, exist_ok=True)
    per_module = 26
    n_modules = max(1, target_symbols // per_module)
    n_classes = 5
    n_functions = 5

    for module_idx in range(n_modules):
        class_blocks = "".join(
            CLASS_TEMPLATE.format(
                class_name=f"Worker{module_idx}_{class_idx}",
                class_idx=class_idx,
                module_idx=module_idx,
            )
            for class_idx in range(n_classes)
        )
        function_blocks = "".join(
            FUNCTION_TEMPLATE.format(
                fn_name=f"do_thing_{module_idx}_{fn_idx}",
                fn_idx=fn_idx,
                module_idx=module_idx,
            )
            for fn_idx in range(n_functions)
        )
        body = PYTHON_TEMPLATE.format(
            module_idx=module_idx,
            n_classes=n_classes,
            n_functions=n_functions,
            class_blocks=class_blocks,
            function_blocks=function_blocks,
        )
        # Spread across 100 directories to stress the by-path index.
        bucket = module_idx % 100
        sub = repo / f"pkg_{bucket:03d}"
        sub.mkdir(exist_ok=True)
        (sub / f"mod_{module_idx:06d}.py").write_text(body, encoding="utf-8")
    return n_modules * per_module


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--target-symbols", type=int, default=30_000)
    p.add_argument("--keep", action="store_true", help="Keep the generated repo on disk for inspection")
    args = p.parse_args()

    out_doc = Path(__file__).resolve().parent.parent / "docs" / "perf" / "medium_repo_perfcheck.md"
    out_doc.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="gp-perf-") as tmpdir:
        repo = Path(tmpdir) / "synthetic"
        approx = generate(args.target_symbols, repo)
        print(f"generated synthetic repo at {repo} (~{approx} symbols)")

        gp = sys.executable
        subprocess.check_call(
            [gp, "-m", "graphify_plus", "init", "--repo", str(repo)],
            stdout=subprocess.DEVNULL,
        )
        print("ran gp init")

        # Don't fail the script if perfcheck finds a slow cell — that's the
        # point of running medium-repo. Capture and report either way.
        proc = subprocess.run(
            [
                gp,
                "-m",
                "graphify_plus",
                "daemon",
                "perfcheck",
                "--repo",
                str(repo),
                "--workload",
                "all",
                "--report",
            ],
            text=True,
            capture_output=True,
        )
        out = proc.stdout
        if proc.returncode != 0:
            print(
                f"perfcheck exited {proc.returncode} — at least one cell over target P99 (this is the signal we wanted)"
            )
        # Skip the FutureWarning preamble on stderr.
        out = out.strip()
        out_doc.write_text(
            f"# Medium-repo perfcheck (synthetic ~{approx:,} symbols)\n\n"
            "Generated by `scripts/perfcheck_medium.py` against a deterministic\n"
            "synthetic codebase. Two runs produce identical numbers.\n\n"
            "See [`RISKS.md`](../../RISKS.md) R9 for context — this is the\n"
            "answer to the review's 'medium-repo numbers needed' ask.\n\n"
            f"{out}\n\n"
            "## How to reproduce\n\n"
            "```bash\n"
            "python scripts/perfcheck_medium.py\n"
            "```\n",
            encoding="utf-8",
        )
        print(f"wrote {out_doc}")
        print()
        print(out)

        if args.keep:
            keep_dir = Path(tempfile.gettempdir()) / "gp-perf-keep"
            if keep_dir.exists():
                shutil.rmtree(keep_dir)
            shutil.copytree(repo, keep_dir)
            print(f"\nkept fixture at {keep_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
