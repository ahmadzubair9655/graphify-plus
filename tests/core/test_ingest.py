from pathlib import Path

from graphify_plus.core.ingest import ingest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


def test_ingest_polyglot_fixture():
    res = ingest(FIXTURE, parallel=False)
    assert res.files_parsed >= 4  # py, ts, tsx, go at minimum
    assert any(s["language"] == "python" for s in res.symbols)
    assert any(s["language"] == "typescript" for s in res.symbols)
    assert any(s["language"] == "go" for s in res.symbols)
    qnames = {s["qualified_name"] for s in res.symbols}
    assert "invoice.Invoice" in qnames
    assert "invoice.Invoice.total" in qnames
    assert "CheckoutButton.CheckoutButton" in qnames


def test_ingest_is_deterministic():
    a = ingest(FIXTURE, parallel=False)
    b = ingest(FIXTURE, parallel=False)
    assert [s["id"] for s in a.symbols] == [s["id"] for s in b.symbols]
    assert [(e.get("src"), e.get("dst"), e.get("kind")) for e in a.edges] == [
        (e.get("src"), e.get("dst"), e.get("kind")) for e in b.edges
    ]


def test_ingest_skips_deny_dirs(tmp_path: Path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "evil.js").write_text("function bad(){}")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("def ok(): pass\n")
    res = ingest(tmp_path, parallel=False)
    paths = {s["path"] for s in res.symbols}
    assert any(p.endswith("ok.py") for p in paths)
    assert not any("node_modules" in p for p in paths)
