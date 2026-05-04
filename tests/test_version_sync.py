"""Guard against version drift between pyproject.toml and the package.

The 5.1.0 release prep surfaced that ``graphify_plus.__version__`` had
sat at ``"3.1.1"`` while ``pyproject.toml`` was at ``5.0.4`` — five
major-line releases of in-package mislabelling. This test runs as part
of the standard ``pytest`` suite (which CI already invokes) so the
mismatch can't reappear silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

import graphify_plus

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_version() -> str:
    with _PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    project = data.get("project") or data.get("tool", {}).get("poetry", {})
    return project["version"]


def test_pyproject_version_matches_package_version():
    pp = _pyproject_version()
    pkg = graphify_plus.__version__
    assert pp == pkg, (
        f"Version mismatch: pyproject.toml={pp} but graphify_plus.__version__={pkg}. "
        f"Update graphify_plus/__init__.py.__version__ to match pyproject.toml."
    )
