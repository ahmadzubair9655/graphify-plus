# Post-merge risk register

The failure modes most likely to surface against the 🟢-shipped layers
once this lands in real Claude Code sessions. Each entry: **symptom**
the user sees, **root cause**, **mitigation already in place**, and
**follow-up** if mitigation isn't enough.

This file is meant to be read by the next person on the project. None
of these are blockers; all of them are the kind of thing you find out
once real workloads hit the code.

---

## R1 — Watcher under heavy churn

**Symptom**: daemon CPU spike + temporarily-stale graph during a
`git pull` / branch switch / large rename refactor that touches
hundreds of files.

**Root cause**: the watcher's 50ms coalescing window was tuned for
interactive editing (one save → one rebuild); it isn't adaptive. A
600-file branch switch fires 600 events in <100ms, all of which
coalesce into a single rebuild — but the next mtime stat-loop sees
600 stale paths and the freshness contract reports `STALE_FILES` for
the duration of the rebuild.

**Mitigation in place**: the rebuild itself is single-threaded under
`_refresh_lock`, so concurrent triggers don't stampede. Hybrid grep
fallback (Layer 2.3) keeps responses useful while stale.

**Follow-up**: adaptive coalescing window (start at 50ms, expand to
500ms when >50 events arrive in a 100ms window). Track in
`graphify_plus/runtime/watcher.py`.

---

## R2 — Daemon crash mid-rebuild

**Symptom**: subsequent `gp daemon start` either rebuilds from
scratch (slow on large repos) or — worse — loads an inconsistent
SQLite cache and serves subtly-wrong answers.

**Root cause**: the existing `runtime/store.py` runs a
`PRAGMA quick_check` and quarantines `cache.db.corrupt.<ts>` on
failure, but a *partial* commit during ingest can leave the cache
internally consistent yet semantically wrong (some symbols updated,
some not).

**Mitigation in place**: `replace_all` runs in a single
`BEGIN IMMEDIATE` transaction with bounded backoff; partial state
should be impossible at the SQLite level.

**Follow-up**: write to `cache.db.next` and atomic-rename only on
success, instead of in-place updates. Adds copy-cost on every
rebuild but eliminates partial-state risk entirely.

---

## R3 — Multi-agent MVCC under concurrent writes

**Symptom**: occasional missing writebacks when two agents annotate
the same node within the same millisecond. Hard to reproduce; will
look like "I told Claude to remember X but it's not in the audit log."

**Root cause**: `multi_agent.py` serialises writes per-target, but
the race between "did I get the lock" and "is my snapshot current"
isn't tested under concurrent agent workloads.

**Mitigation in place**: per-target write locks via
`SnapshotRegistry.acquire_write_lock`; audit log is append-only so
no overwrites are possible.

**Follow-up**: write a chaos test that drives ≥4 concurrent agents
through 1k writes each and asserts every write reaches the log.
File in `tests/daemon/test_multi_agent_chaos.py`.

---

## R4 — Embedding cache after rename

**Symptom**: a renamed file with similar content keeps its old
embedding cached (so concept search returns stale rerank scores) or
invalidates and recomputes (slow on first query).

**Root cause**: `embeddings._NODE_CACHE` keys on `(symbol_id,
text_hash)`. A rename produces a new symbol_id, so the cache entry
for the old id leaks until process restart. A *renamed file with
unchanged content* keeps the same body but a new `path` field, so
the text-hash check picks up — but the cache footprint grows.

**Mitigation in place**: lazy recompute on text-hash mismatch; stale
ratio is observable via `embeddings.stale_ratio`.

**Follow-up**: bound `_NODE_CACHE` size with an LRU and emit
`stale_ratio` into the freshness envelope so callers see it.

---

## R5 — Freshness contract under interrupted rebuilds

**Symptom**: the freshness token reports `FRESH` but a query returns
incomplete results because the rebuild was killed mid-flight.

**Root cause**: `freshness_token` is computed from
`(built_at, n_symbols, n_edges)`. If a rebuild is killed after the
SQLite commit but before the in-memory snapshot swaps in,
`freshness()` reads the *old* snapshot's token while the underlying
data is partially updated.

**Mitigation in place**: snapshot swap happens under
`_snapshot_lock` after the build completes, so a killed mid-build
doesn't expose a half-built snapshot.

**Follow-up**: add a checksum to `built_at` that includes the
sha256 of the cache file at build start; if the cache changes
between build start and snapshot swap (i.e. a concurrent watcher
update), invalidate and retry.

---

## R6 — The hook noise floor

**Symptom**: hook nudges become ignored after the first dozen.
Claude (or the user) starts grepping anyway.

**Root cause**: every bareword grep against a FRESH graph with
matches gets a nudge. In a session with many greps, that's 50+
nudges in an hour. Beyond ~5 or 6 the diminishing-returns kick in.

**Mitigation in place**: the hook only fires for FRESH graphs,
bareword patterns, and nonzero matches. Stale paths and regex stay
silent.

**Follow-up**: implement a per-session nudge budget (say, 1 nudge
per 5 grep calls per `cwd`) by remembering recent nudge timestamps
in `.graphify_plus/hook-state.json`. Add a "snooze" command. Track
acceptance rate via `gp daemon adoption` and tune the budget.

---

## R7 — Ingestor data drift

**Symptom**: ingest from GitHub / Slack / coverage runs once,
becomes stale over days, no signal that it should re-run.

**Root cause**: ingest is on-demand. No cron, no webhook, no
"this is N days old" badge. The freshness contract covers code,
not external sources.

**Mitigation in place**: `ingestors` table has `ingested_at`;
`load_ingest_nodes` returns it.

**Follow-up**: extend the freshness envelope to surface external-
source staleness (e.g. `external_freshness: {github: "3d", coverage:
"7d"}`). Add `gp daemon ingest --refresh-stale` that re-pulls any
source older than its policy threshold.

---

## R8 — The benchmark harness corpus is too small

**Symptom**: the precision/recall numbers from `gp daemon benchmark`
are technically true but rest on one tiny fixture; reviewers can't
tell if the rewrite generalises.

**Root cause**: building a curated multi-repo corpus with hand-
labelled gold graphs is real work that wasn't in scope.

**Mitigation in place**: benchmark harness *machinery* is shipped
and works; declarative format is documented; built-in tiny corpus
exists.

**Follow-up**: pick 3 mid-sized OSS repos (Flask, Express,
NumPy-style), spend 1 person-week labelling gold answers, ship
`tests/fixtures/benchmark_corpus/` with real entries. Publish
precision/recall in the v6.0.0 release notes.

---

## R9 — The 0.2ms claim is workload-dependent

**Symptom**: a reviewer runs `gp daemon perfcheck --workload all`
on a 100k-node monorepo and finds cells over 100ms.

**Root cause**: the original P50 = 0.2ms claim was on
`callers_of(random_sid) + find_by_name(name, limit=8)` on a 1,371-
symbol repo with hot caches. That workload is real but it isn't
the worst case.

**Mitigation in place**: 12-cell perfcheck table now runs and
reports cold/warm/hot × 4 workloads. The actual numbers on a
1,371-symbol repo are all <0.4ms P99. Larger repos haven't been
measured in this branch.

**Follow-up**: run perfcheck on a 30k and a 100k node monorepo,
commit the resulting tables to `docs/perf/`. If any cell exceeds
target, that's the next optimization.

---

## R10 — Hook adoption telemetry needs the hook installed

**Symptom**: `gp daemon adoption` reports `0 grep_calls` even on
repos where Claude is grepping all the time.

**Root cause**: `adoption_report` reads `grep-events.jsonl`, which
is only written by the pre-grep hook. If the hook isn't installed
in `.claude/hooks/` and registered in `settings.json`, no greps
are observed.

**Mitigation in place**: `gp daemon install` writes the hook
script; `gp daemon quickstart` invokes it. The README's quickstart
section walks through the settings.json snippet.

**Follow-up**: add `gp daemon doctor` (a future command) that
checks `.claude/settings.json` for the hook registration and warns
when it's missing. Today the user has to read the help text and
remember.

---

## R11 — mypy `continue-on-error: true` is technical debt

**Symptom**: type errors land in main without anyone noticing.

**Root cause**: 18 mypy errors remain (down from 36 after
`types-PyYAML` install). Most are Symbol-vs-`dict[str, Any]`
generics that need either a real refactor or per-line ignores.

**Mitigation in place**: CI continues to run mypy and surface the
errors; `continue-on-error: true` keeps the rest of the build green.

**Follow-up**: open a tracking issue per remaining error or do the
2-day generics refactor and flip `continue-on-error: false`. Either
way, this should not stay tolerated indefinitely.

---

## R13 — 1-hop tail latency on medium graphs (empirically found)

**Symptom**: on a 38k-symbol synthetic codebase the `1_hop` workload
shows P99 = 1.78s in the warm/hot cache states (P50 stays at
~0.01ms). A session with hundreds of `who_calls` calls per hour
will hit visible delays on the long tail.

**Root cause**: `_inbound` does a per-call linear scan for placeholder
aliases keyed on the target's short name. For names like `compute`
that map to hundreds of placeholders, iterating their inbound edges
is the linear-in-placeholder-fanout cost. Most calls miss the tail;
the 99th-percentile common-name calls hit it.

**Mitigation in place**: median latency is fine; the tail is a
correctness-preserving slow path, not wrong answers. Hot-cache P99
on the small repo (1,371 symbols) is 0.4ms — the issue scales with
graph size.

**Follow-up**: precompute the placeholder→callers join at snapshot
build time so query-time becomes O(1) instead of O(fanout). Target
v6.0.1. Until then, document this in release notes so reviewers see
the medium-repo number honestly.

**Reproduce**:

```bash
python scripts/perfcheck_medium.py --target-symbols 30000
cat docs/perf/medium_repo_perfcheck.md
```

**Methodology caveat**: with `samples_per_cell=25` the P99 is
effectively the max of 25 samples. Re-run with `--samples 1200`
(100 per cell) for a real P99 distribution. The current numbers are
a signal, not a precise measurement.

---

## R12 — Slack ingest privacy boundary

**Symptom**: a user without an explicit `chat-allowlist.yaml` runs
the ingest expecting it to "just work" and is surprised by the
silent skip.

**Root cause**: by design — the privacy contract says no channels
are ever ingested without explicit allow-list. But the surprise
might cause a user to think the feature is broken.

**Mitigation in place**: when `ingest_slack` returns `skipped: True`,
the CLI prints a clear "configure
`.graphify_plus/chat-allowlist.yaml`" message.

**Follow-up**: add an interactive `gp daemon ingest slack init`
that walks the user through allow-list creation with informed
consent prompts.

---

The pattern across all twelve: each shipped feature is correct under
the conditions tested; the risks are about *conditions not yet
tested*. Most resolve with a single follow-up issue + a few hours of
work. The job of this register is to make sure they're not silently
forgotten.
