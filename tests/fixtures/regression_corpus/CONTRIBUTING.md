# Expanding the regression corpus

This corpus catches accidental regressions in adapter parsing — adding,
removing, or renaming a single symbol or edge in any file flips a test.

## Adding files

1. Drop a new file under the matching language directory. Keep it small
   (under ~30 lines) and self-contained — exercise **one** language
   feature per file (e.g. `06_pattern_matching.py`, not a kitchen-sink).
2. Prefer original code over copying. If you must derive from an
   upstream open-source file, record it in `SOURCES.md` with license +
   SPDX identifier.
3. Regenerate baselines with:
   ```
   python -m scripts.regen_corpus_baselines
   ```
   Review the diff in `baselines.json` carefully — it's the contract.
4. Commit corpus + baseline changes together.

## Eventual target

The full RESILIENCE_PLAN target is ≥30 files per language (~240 total).
v4.2.0 ships a 5-file-per-language starter; growth happens incrementally
in subsequent PRs.
