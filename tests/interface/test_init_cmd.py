import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "graphify_plus", *args],
        capture_output=True,
        check=False,
    )


def test_gp_init_writes_cache_and_jsonl(tmp_path: Path):
    # Copy fixture to tmp so cache files don't pollute the source tree.
    import shutil

    target = tmp_path / "repo"
    shutil.copytree(FIXTURE, target)
    r = _run("init", "--repo", str(target), "--no-parallel")
    assert r.returncode == 0, r.stderr.decode()
    assert (target / ".graphify_plus" / "cache.db").exists()
    jsonl = target / ".graphify_plus" / "graph_symbols.jsonl"
    assert jsonl.exists()
    head = jsonl.read_bytes().splitlines()[0]
    assert b'"kind":"header"' in head


def test_gp_init_print_is_deterministic(tmp_path: Path):
    a = _run("init", "--repo", str(FIXTURE), "--print", "--no-parallel")
    b = _run("init", "--repo", str(FIXTURE), "--print", "--no-parallel")
    assert a.returncode == 0 and b.returncode == 0
    assert a.stdout == b.stdout
