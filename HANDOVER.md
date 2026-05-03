# HANDOVER — graphify-plus continuation

> **Read this entire file before doing anything else.** Context from a 600k-token session that built EXECUTION_PLAN Phases 0–10 (v4.0.0) and most of RESILIENCE_PLAN Phase 11 (v4.1.0 — PR #13 awaiting CI). The continuation session is expected to start blind.

---

## 0. Working directory rule (load-bearing)

**Work exclusively in `~/dev/graphify-plus`.** This Claude session was launched from inside an unrelated project's working directory (`smart-energie`, a UK domestic-retrofit website). The new session will likely be launched from the same place. **Ignore that codebase entirely.** Do not edit it, browse it, or reference it. Every command must `cd ~/dev/graphify-plus` first or use absolute paths into it.

`smart-energie/CLAUDE.md` belongs to a different project and is irrelevant to graphify-plus development.

---

## 1. What graphify-plus is

A standalone CLI + MCP-server tool for indexing arbitrary codebases into a symbol-level graph that AI coding agents query. It points at any **target repository** with `--repo <path>` and writes everything it generates into that target's `.graphify_plus/` directory at runtime. **graphify-plus's own tree never receives runtime artefacts.** This invariant is observed by every phase shipped to date and must continue to hold.

Public repo: https://github.com/ahmadzubair9655/graphify-plus

---

## 2. Where the project is right now

### Tags pushed (`git tag -l` to confirm)
- `v3.5.0` — after Phase 5 (manifest + sync-docs daemon)
- `v4.0.0` — after Phase 10 (MCP server + claude-md). EXECUTION_PLAN.md complete.

### Open PRs / branches
- **PR #13** (`feat/phase-11-resilience`) — Phase 11 foundation block (v4.1.0). **Awaiting CI; auto-merge on green is authorised.** This contains tasks 11.0, 11.1, 11.3, 11.4, 11.6.
  - 11.0 — TestCentralityDrift verified deterministic across 5 runs (numpy + scipy already in deps from Phase 0). Inherited debt closed.
  - 11.1 — `interface/errors.py` defines `GraphifyError` + 9 concrete subclasses with stable codes (`GP-CACHE-CORRUPT`, etc.). Dispatcher in `__main__.py` catches via `handle()`, prints structured stderr, dumps context to `<repo>/.graphify_plus/debug.log`, exits with per-code value. `--debug` flag (or `GP_DEBUG=1`) prints traceback.
  - 11.3 — `runtime/store.py` cache resilience: `PRAGMA quick_check` on open with quarantine to `cache.db.corrupt.<utc-ts>`; schema versioning (`SCHEMA_VERSION = 1`); `BEGIN IMMEDIATE` with 5-retry exponential backoff before `GP-CACHE-LOCKED`; `Store.backup()` via SQLite backup API.
  - 11.4 — `core/ingest.py` per-file parse isolation: `GP_PARSE_TIMEOUT_SEC=5`, `GP_PARSE_MEM_MB=512` (POSIX RLIMIT_AS), failed files get a stub symbol with `parse_status="failed"` and entry in `<repo>/.graphify_plus/skipped.jsonl`. `--strict` flag fails-fast for CI.
  - 11.6 — `gp doctor` diagnostic CLI: System / Cache / Adapter coverage / Recent errors sections, exits 1 on any `ERROR` severity, `--json` for machine output.

### Merged PR history (chronological, all on `main`)
| # | Squash SHA | Title |
|---|---|---|
| 2 | `be78ce0` | feat(core): universal Tree-sitter frontend & foundation refactor |
| 3 | `8c6f9a2` | feat(core): skeletonizer & content-addressed skeleton store |
| 4 | `f064035` | feat(runtime): live AST watcher & delta-memory sessions |
| 5 | `7fb2559` | feat(query): topological partitioning & token-budgeted framing |
| 6 | `7ebdff2` | feat(query): semantic neighbourhood discovery & dead-code pruning |
| 7 | `e32b5f1` | feat(query): STAGING_PLAN manifest generator & sync-docs daemon |
| 8 | `21868cb` | feat(runtime,query): unified constraint engine — shadow, guardrails, drift |
| 9 | `ea34f4c` | feat(query,interface): cross-boundary meta-graph & multi-agent orchestrator |
| 10 | `0904333` | feat(query,interface): git/telemetry/vuln/lockfile enrichment + privacy filter |
| 11 | `1c407db` | feat(query,interface): edge-centric test generation & visual spatial output |
| 12 | `a082d21` | feat(interface): MCP server, CLAUDE.md auto-gen, v4.0.0 release |

### Test count
**223 tests passing** as of PR #13 head. Run with `pytest -q --timeout=30` from the repo root inside the venv at `~/dev/graphify-plus/.venv/`.

---

## 3. Immediate next action (the user's exact instruction, verbatim)

The user's most recent message — paste these three numbered points back to them once you have something to say. **Do not start v4.2.0 work until you have made the three-point assessment and the user has explicitly authorised the next PR.**

> Approved. Auto-merge PR #13 once CI is green, tag v4.1.0, then pause exactly as planned.
>
> On the v4.2.0 scope decision — the split is correct. The deferred items (11.2 confidence layer, 11.5 regression corpus, 11.7 adversarial probes, 11.8 telemetry, 11.9 doc verification, 11.10 wizard + gp explain, all of Phase 12) are substantial and deserve their own PR with full review. Do not rush them into v4.1.0.
>
> When you come back after tagging v4.1.0, propose a sequencing plan for the remaining items before starting any code. Specifically tell me:
>
> 1. Which of the deferred 11.x items have hard dependencies on each other and what order they must go in
> 2. Whether Phase 12 has any items that can run in parallel with the remaining 11.x work or whether Phase 12 must wait for all 11.x to land first
> 3. Your estimate on whether the remaining work fits in one PR or needs a further split
>
> That three-point assessment first, then I will give you the go-ahead to start v4.2.0 work.
>
> One thing to confirm when you tag v4.1.0 — verify that `gp doctor` reports clean on a fresh `gp init` of the sample fixture before pushing the tag. Do not tag on CI green alone for a release; run the doctor check manually first.

### Concrete steps for the next session, in order

1. `cd ~/dev/graphify-plus && source .venv/bin/activate && gh pr checks 13`
2. If still pending: `ScheduleWakeup` for 270s with `prompt: <this same step>`. If failing: read logs (`gh run view --log-failed <run-id> --job <job-id>`), fix, `git push`, ScheduleWakeup 270s.
3. When all 6 matrix combos green:
   ```
   gh pr merge 13 --squash --delete-branch
   git switch main && git pull --ff-only
   ```
4. **Manual `gp doctor` check on fresh init (mandatory before tag):**
   ```
   rm -rf tests/fixtures/sample_repo/.graphify_plus
   python -m graphify_plus init --repo tests/fixtures/sample_repo --no-parallel
   python -m graphify_plus doctor --repo tests/fixtures/sample_repo
   ```
   Verify exit code 0 and **no `ERROR` severity lines** in the report. Quarantined-cache rows are acceptable only if you understand why they exist; on a fresh init they should be absent.
5. Only if doctor is clean: `git tag -a v4.1.0 -m 'v4.1.0 — resilience foundation' && git push origin v4.1.0`. Cleanup the fixture artefacts (`rm -rf tests/fixtures/sample_repo/.graphify_plus`).
6. **Stop.** Produce the three-point assessment of the deferred work (11.2, 11.5, 11.7, 11.8, 11.9, 11.10, plus all of Phase 12). Do **not** start coding the v4.2.0 PR until the user replies.

---

## 4. The deferred items — context for the three-point assessment

Read `RESILIENCE_PLAN.md` (in this repo's parent? see §6 for source plan locations) for the full descriptions. Quick recap of the deferred work:

- **11.2** — Confidence propagation. Every edge produced by every adapter gets a `confidence: float` field. Default values per source: 1.0 contract / Tree-sitter exact, 0.7 import-resolution heuristic, 0.5 cross-language stitch, 0.4 telemetry/community-inferred, 0.2 fallback. Skeletonizer annotates low-confidence edges. `gp context --min-confidence FLOAT` filters. CLAUDE.md template gains a confidence-warning paragraph. **Touches every adapter file plus `core/skeletonizer.py`, `interface/cli/context_cmd.py`, `interface/cli/claude_md_cmd.py`.**
- **11.5** — Adapter regression corpus. ≥30 real-world files per language under `tests/fixtures/regression_corpus/<language>/` (Python, TS, JS, Go, Rust, Java, Ruby, C# = 8 × 30 = ~240 files), with attribution, plus `baselines.json` of expected symbol counts. CI fails on regression. **Mostly fixture sourcing + a single regression test driver.**
- **11.7** — Adversarial audit expansion. Five new probes in `audit/probe.py`: stale-edge, phantom-symbol, cross-language stitch, unresolved-import, skeleton-source-divergence. Adds 0–100 trust score to `gp audit` banner. Warning line when trust < 70.
- **11.8** — Opt-in telemetry + `gp report-error`. New `interface/telemetry.py`. Disabled by default. Sends only command name, duration, cache size bucket, file count bucket, error code, version, OS, Python version. Configurable endpoint; never sent if `GRAPHIFY_TELEMETRY_URL` unset. `gp report-error` shows redacted traceback to user before they consent to send.
- **11.9** — Documentation verification. `scripts/test_readme.py` parses every fenced code block in README/CLAUDE.md/docs, runs the bash blocks in a sandbox, fails CI on non-zero or output mismatch. Versioned CLAUDE.md template marker (already partially in place — `<!-- graphify-plus-template-version: 1 -->`). `scripts/gen_docs.py` extracts CLI help → README sections.
- **11.10** — UX hardening. First-run wizard when `gp init` runs interactively in a fresh repo (stack detection, ignore suggestions, optional CLAUDE.md/watcher kick-off). Error-remediation rendering (already largely shipped in 11.1 — but verify all error paths actually populate `remediation`). New `gp explain <symbol>` command: skeleton + 1-hop neighbours + community + audit warnings + git history + reverse-BFS entry points.
- **Phase 12** (all of it) — Watcher reconciliation pass (mtime sweep + initial sweep + filesystem auto-detection + git-hook bridge); repo-level locking (file-based shared/exclusive `.graphify_plus/.lock`); resource discipline (hard ceilings + adaptive ignore + `gp vacuum`); feedback loop (disputed edges from failed audits, confidence drift tracker); security hardening for hosted mode (sandboxed parsing + skeleton sanitisation + read-only default + rate limits); versioned cache compatibility matrix; REST/HTTP server (`gp serve`); web UI (Cytoscape.js graph explorer at `/ui/`).

### My pre-formed view (the user has not yet seen this — don't paste it as the assessment, but use it as a starting point)

**Hard dependencies inside 11.x:**
- 11.2 (confidence layer) is a foundation for 11.7 (audit probes use confidence scores), 11.8 (telemetry records confidence buckets), 11.10 (`gp explain` displays confidence on neighbours).
- 11.5 (corpus) and 11.9 (doc verification) are independent of everything else.
- 11.7 depends on 11.2.
- 11.10's `gp explain` reads everything and is a leaf — should be last.

**Phase 12 dependencies on 11.x:**
- 12.1 (watcher reconciliation) is independent of every 11.x item — could land before any of them.
- 12.2 (concurrency / repo locking) layers on top of 11.3's `BEGIN IMMEDIATE`. Independent of other 11.x.
- 12.3 (resource ceilings + `gp vacuum`) independent.
- 12.4 (feedback loop / confidence drift) depends on 11.2 (confidence layer) and 11.7 (audit probes that emit "disputed" markers).
- 12.5 (security hardening, hosted mode) requires 12.7 (REST) to mean anything.
- 12.6 (versioned cache compat matrix) extends 11.3.
- 12.7 (REST) independent.
- 12.8 (web UI) requires 12.7 (REST).

**Recommended PR split (my proposal — but the user might prefer differently):**
- v4.2.0 — 11.2 + 11.5 + 11.9. Confidence layer touches every adapter and is the foundation for 11.7/12.4. Corpus + doc verification are independent.
- v4.3.0 — 11.7 + 11.8 + 11.10 + 12.4. Audit probes + telemetry + explain + feedback loop. All depend on confidence (11.2) which v4.2.0 ships.
- v4.4.0 — 12.1 + 12.2 + 12.3 + 12.6. Watcher reconciliation + locking + resource discipline + cache compat. Operational hardening.
- v4.5.0 — 12.7 + 12.5 + 12.8. REST + sandboxing + web UI. Hosted-mode tier — only ship if the user confirms there's demand for hosted mode.

**Single-PR cost estimate:** Cramming all of v4.2.0–v4.5.0 into one PR would be ~3000+ LoC of net new code, ~80 new tests, ~240 fixture files. Not reviewable. Strongly recommend the four-step split above.

---

## 5. Operating norms learned over this session

The user gave several explicit and several implicit norms. **Honour them.**

### Explicit
- **Auto-merge once CI is green.** Was authorised after Phase 0 and applies to every non-release PR. Manual gate only on tagged releases (Phase 5/v3.5.0, Phase 10/v4.0.0, but **v4.1.0 was authorised auto-merge** because no PyPI publish workflow exists yet). For v4.2.0+, treat the same — auto-merge unless the user explicitly says otherwise.
- **Pause before each release tag.** Even with auto-merge authorised on the PR, run any pre-flight checks the user requested. For v4.1.0 specifically: run `gp doctor` on a fresh fixture init before tagging. **Do not tag on CI green alone.**
- **Write generated artefacts only into the target repo at runtime.** Never into graphify-plus's tree. This was stated up-front and reaffirmed at every phase. Test fixtures are an exception (they are the target).
- **§4.5 of the original EXECUTION_PLAN.md is dropped permanently.** It mandated baking commercial copy phrases ("wide range of leading boiler brands", "Best Price Guarantee") into mock data, fixtures, generated CLAUDE.md, etc. The user dropped this on day one. Do not reintroduce. The §9 step 10 terminology assertion in the E2E test is also dropped.
- **Do not blanket-lint legacy modules.** Each phase lints only what it touches. CI scope is `graphify_plus/{core,runtime,interface,query} scripts tests/{core,runtime,interface,query}`. The legacy `graphify_plus/audit/` and `graphify_plus/review/` plus `tests/test_audit_production.py` and `tests/test_diff_production.py` are out of scope; clean them up only when a phase legitimately touches them.
- **Do not fragment the plan.** When the user asked whether to start RESILIENCE_PLAN early on top of partial EXECUTION_PLAN, I (correctly) declined and the user agreed. Same posture for v4.2.0 — propose the assessment, wait for go-ahead.
- **No PyPI auto-publish on first tag.** When v4.0.0 shipped, the user explicitly chose **not** to add `release.yml` for OIDC trusted publishing. It will be a separate decision after manual install verification. Do not add it without explicit authorisation. Same for v4.1.0.

### Implicit (learned from corrections)
- Surgical changes only. Touch what the task requires. No drive-by refactors.
- Lint/format autofix is OK; large mass reformats of unrelated files are not.
- `STAGING_PLAN_*.md` should be cleaned up from the fixture before commits (zsh glob hates `STAGING_PLAN*.md`; use explicit names).
- `tests/fixtures/sample_repo/.graphify_plus/` must be deleted before the determinism check and before `git add -A`.
- Use `pytest --timeout=30` once `pytest-timeout` is installed (it is now, in dev extras).
- `time.sleep` is forbidden in watcher tests — use `Watcher.wait_for_path()`.
- Network failures on `gh` calls are common — retry, don't assume failure.
- The merge call has timed out before completing more than once (PR #8). Always verify with `gh pr view <N>` showing `state: MERGED` before assuming.

---

## 6. Source plan documents

These are the user's specs. Read them — they're the authority on what each task requires.

- `~/Downloads/EXECUTION_PLAN.md` — Phases 0–10. **Complete.** Reference only.
- `~/Downloads/RESILIENCE_PLAN.md` — Phases 11 + 12. The deferred items live here. The user pasted Phase 11 inline at the time it became relevant; Phase 12 is in this file as well. **Re-read it before drafting the three-point assessment.**

These are read-only inputs from the user's environment. Don't write into them.

---

## 7. Useful commands cheat-sheet

```bash
# Activate env
cd ~/dev/graphify-plus && source .venv/bin/activate

# Full test run
pytest -q --timeout=30

# Lint Phase 0+ scope (matches CI)
ruff check graphify_plus/core graphify_plus/runtime graphify_plus/interface graphify_plus/query scripts tests/core tests/runtime tests/interface tests/query
ruff format --check graphify_plus/core graphify_plus/runtime graphify_plus/interface graphify_plus/query scripts tests/core tests/runtime tests/interface tests/query

# Determinism check (must always pass before commit)
rm -rf tests/fixtures/sample_repo/.graphify_plus
python -m graphify_plus init --repo tests/fixtures/sample_repo --print 2>/dev/null > /tmp/a.jsonl
python -m graphify_plus init --repo tests/fixtures/sample_repo --print 2>/dev/null > /tmp/b.jsonl
cmp /tmp/a.jsonl /tmp/b.jsonl && echo OK

# Perf bench
python scripts/perf_bench.py --baseline    # writes baseline
python scripts/perf_bench.py --check       # CI gate (15% regression budget)

# Doctor on fresh fixture (mandatory before v4.1.0 tag)
rm -rf tests/fixtures/sample_repo/.graphify_plus
python -m graphify_plus init --repo tests/fixtures/sample_repo --no-parallel
python -m graphify_plus doctor --repo tests/fixtures/sample_repo
echo "exit $?"

# CI status
gh pr checks <PR-number>

# Merge a green PR
gh pr merge <N> --squash --delete-branch
git switch main && git pull --ff-only

# Tag a release
git tag -a v<X.Y.Z> -m "v<X.Y.Z> — <one-line>"
git push origin v<X.Y.Z>
```

---

## 8. Files written this session you should NOT modify casually

- `pyproject.toml` — version is currently `4.1.0`. Each release bumps it. Base deps include numpy, scipy, tree-sitter, click, rich, pydantic, watchdog, pathspec, diskcache, orjson, xxhash, GitPython, tiktoken, rank-bm25, PyYAML, Jinja2. Extras: `embeddings`, `mcp`, `viz`, `telemetry`, `dev`.
- `CHANGELOG.md` — keep entries chronological; v4.1.0 is at the top above v4.0.0.
- `.github/workflows/ci.yml` — Python 3.10/3.11/3.12 × ubuntu/macos. Lint scoped to Phase 0+ paths. Determinism + perf-baseline steps.

---

## 9. If you're stuck

- Don't guess at the user's intent. Ask. The user has been responsive and prefers a one-shot question over a PR that misreads scope.
- The plan document is the authority. If the plan and a session memory disagree, re-read the plan section.
- The user dislikes shipping incomplete work. Better to ship 11.0/1/3/4/6 cleanly (as we did) than to ship all of 11.x with cracks.
- Run all gates locally before pushing, even when CI catches it. CI feedback loop is ~3min; local feedback is ~7s. Use the local one.

---

## 10. The four pillars (architectural reminder)

The codebase is organised into four pillars per `EXECUTION_PLAN.md` §1.4. Don't let modules drift across pillars without thinking.

```
A · INGEST    — graphify_plus/core/    (adapters, ingest, symbol_graph, skeletonizer, cas)
B · RUNTIME   — graphify_plus/runtime/ (store, watcher, session, overlay, rules)
C · QUERY    — graphify_plus/query/   (partition, budget, semantic, prune, manifest, sync_docs,
                                        guardrails, shadow, drift, stitch, telemetry, git_silo,
                                        blast_radius, lockfile, tests_gen)
D · INTERFACE — graphify_plus/interface/ (cli/*, mcp_server, orchestrator, visual, errors, privacy)
```

If you find yourself adding a query-shaped thing under `runtime/`, stop and reconsider.

---

End of handover. Read RESILIENCE_PLAN.md for §4 detail before drafting the three-point assessment.
