# Changelog

All notable changes to graphify-plus.

## Unreleased

### Added

- **In-memory graph daemon** (`graphify_plus/daemon/`). A long-running
  local process that holds the symbol graph in RAM with pre-computed
  indexes (label trie, inverted-text index, 1-hop adjacency cache,
  PageRank top-N, communities) so common queries return in
  sub-millisecond P50 instead of paying the SQLite + NetworkX cold-start
  cost on every call. Bound to a Unix domain socket under
  `$TMPDIR/gp-<hash>.sock` (short path keeps macOS `AF_UNIX` happy).
  Surfaces a JSON-line RPC protocol with a uniform response envelope:
  `{ok, freshness, receipt, results, more_available}`. Lifecycle and
  query commands ship as `gp daemon {start,stop,status,refresh,query}`.
- **Intent-typed tools** (`graphify_plus/daemon/handlers.py`). Each
  handler answers exactly one question and returns rows with
  `{node_id, label, source_file, line_number, snippet, confidence,
  kind}` so callers can pipe straight into Read/Edit without a second
  hop. Shipped: `whats_in`, `who_calls`, `whos_called_by`,
  `what_depends_on`, `what_does_this_depend_on`, `find_by_name`
  (exact → prefix → suffix → short-name → substring confidence
  ladder), `find_by_concept` (cheap inverted-index path + opt-in heavy
  BM25 + community-weighted path), `whats_central`. All responses are
  token-budgeted (default 1500 tokens) with a `more_available` count
  for paging. Unresolved-call placeholders (`self.login`, `s.login`)
  are rolled back into their real symbol so `who_calls` works through
  the existing adapter limitations.
- **Freshness contract** (`graphify_plus/daemon/protocol.py`,
  `graphify_plus/daemon/indexes.py`). Every daemon response includes a
  `freshness` envelope with `trust ∈ {FRESH, LIVE_AHEAD, STALE_FILES,
  STALE_REBUILD_NEEDED}`, a 12-hex `freshness_token` that changes
  whenever the graph changes, the count of files modified since the
  last build, the first 16 stale paths, and a one-line hint when trust
  degrades.
- **Per-call receipts** (`graphify_plus/daemon/receipts.py`). Each
  response carries `{op, elapsed_ms, tokens, grep_equivalent}` so
  Claude sees the in-context win for using the graph
  (`[graphify-plus] who_calls · 0.05ms · 38 tokens · would have taken
  ~3 grep calls + 1 file reads`).
- **MCP intent tools** (`graphify_plus/interface/mcp_server.py`). Eight
  new MCP tools (`gp_whats_in`, `gp_who_calls`, `gp_whos_called_by`,
  `gp_what_depends_on`, `gp_what_does_this_depend_on`,
  `gp_find_by_name`, `gp_find_by_concept`, `gp_whats_central`) route
  through the daemon when running and fall back to an in-process build
  when it isn't, so the same call works regardless of daemon state.

### Docs

- **README rewritten for 5.1.0 surface** (`README.md`,
  `README.legacy.md`). The pre-v4 README documented an "enhancement
  layer over safishamsi/graphify" architecture that no longer exists —
  approximately 80% of its content described modules, APIs, and CLI
  commands that were removed during the v4 replatform. The previous
  README is preserved verbatim as `README.legacy.md` with a header
  noting its historical status. The new `README.md` is written from
  scratch around the verified current surface: 26 CLI subcommands
  grouped by purpose, 10 audit probes, the dead-code classifier, the
  drift rules engine, the five same-file edge kinds, and the actual
  package layout. Every claim was verified against `gp --help`,
  `pyproject.toml`, the probe code, and live tool output before it
  went into the file.
- **README: Dead-code analysis section** (`README.md`). New
  `## Dead-code analysis` section after the audit, covering `gp init`
  + `gp prune` usage, the eight classifier categories with their
  confidence tiers, and `--min-confidence 0.7` for the actionable
  high-signal subset. Plus a brief subsection naming the three
  same-file edge kinds (`jsx_render`, `calls`, `references`) the TS/JS
  adapter resolves during `gp init`. The README's existing v2/v3
  enhancement-pipeline structure is left untouched — additive only.

### CI

- **Version-sync guard** (`tests/test_version_sync.py`). Pytest test
  that fails the build if `pyproject.toml`'s `[project].version` and
  `graphify_plus.__version__` drift apart. Surfaces in any environment
  that runs the standard test suite (no extra CI machinery needed),
  with an error message that names both values so the fix is obvious.
  Motivated by the 5.1.0 release prep, which discovered
  `__version__` had silently sat at `"3.1.1"` for five major-line
  releases.

## 5.1.0 — 2026-05-04

This release makes the prune subsystem genuinely usable on real-world
TypeScript and JavaScript codebases for the first time. Function calls,
JSX render relationships, and value references are now resolved within
a file, so locally-used helpers no longer appear as dead code. Prune
output is classified by confidence, and the new `--min-confidence` flag
filters to high-confidence dead-code candidates only. The audit gains
the `modularity_quality` probe and writes per-node community attributes
during `gp init`. Validated end-to-end against a 451-file production
codebase: high-confidence prune list went from 103 false positives to 0.

### New

- **JSX-internal symbol tracing** (`core/adapters/_ts_common.py`). The
  TS/JS adapter now does a second AST pass over `.tsx`/`.jsx` files
  (and any `.ts`/`.js` file containing JSX), resolving `<Component />`
  references to function or component declarations in the same file.
  Resolved references are emitted as `jsx_render` edges, which
  `gp prune` counts as inbound liveness evidence. The walker also
  descends into function bodies so nested local components become
  symbols. Patterns covered: simple identifier (`<Foo/>`), member
  expression (`<Card.Header/>`), arrow-function components, and
  components declared inside another component's body. Host elements
  (`<div/>`, `<svg/>`) are filtered out.
- **Same-file `calls` edge extraction** (`core/adapters/_ts_common.py`).
  The TS/JS adapter now emits `calls` edges from each enclosing function
  to any same-file symbol it invokes. Handles identifier callees
  (`foo()`), member-expression callees (`Ns.foo()`, `A.B.fn()`), calls
  inside template literal substitutions (`` `${fn(x)}` ``), and calls
  inside JSX expression containers (`<div>{fn(x)}</div>`). Cross-file
  calls stay unresolved on purpose — those are the imports graph's job.
  Edge confidence: `0.7` (CONF_RESOLVED), with a numeric
  `confidence_score` attribute.
- **Same-file `references` edge extraction** (`core/adapters/_ts_common.py`).
  Closes the function-as-value false-positive class: functions stored in
  const arrays (`const SCENES = [Foo, Bar]`), passed as arguments
  (`subscribe(callback)`), returned from other functions, or assigned as
  object-literal values (`{ onClick: handler }`) now get inbound
  `references` edges. Uses parent-context exclusions to skip declaration
  positions, import bindings, JSX element names, member-expression
  property names, call-expression function positions, and destructuring
  patterns. Edge confidence: `0.5` (lower than calls/jsx_render —
  identifier references are noisier signals).
- **Honest dead-code classification in `gp prune`**
  (`query/prune.py`, `interface/cli/prune_cmd.py`). Each candidate now
  carries a `likely_category` and a `confidence` 0.0–1.0. Categories:
  `plausibly_dead` (≈0.85), `prop_type` (≈0.4), `unknown` (≈0.5), and
  the four false-positive-prone tags `jsx_internal`, `reducer_case`,
  `test_internal`, `pytest_fixture`, `dunder_method` (all ≈0.15). New
  `--min-confidence` flag filters to high-signal candidates only — e.g.
  `gp prune --min-confidence 0.7` returns only the genuinely-likely-dead
  symbols. The text output displays category + confidence inline; JSON
  output adds a `dead_classified` array.
- **Per-node community attributes during `gp init`**
  (`interface/cli/init_cmd.py`, `audit/probe.py`). `gp init` now runs
  Louvain community detection at the end of the pipeline and writes a
  `community` attribute onto every symbol — both into the SQLite cache
  and `graph_symbols.jsonl`. Unblocks the `edge_deletion_stability`
  audit probe, which previously skipped on every cache because nothing
  wrote this attribute. With the partial-coverage fix in
  `_community_set` (synthetic `-1` community for placeholder nodes),
  `modularity_quality` also runs to completion. For backwards
  compatibility with caches generated before this fix, the audit
  derives Louvain on-the-fly when no node carries a `community`
  attribute (no re-`gp init` required).

### Compatibility

Additive only. Existing `cache.db` files and `graph_symbols.jsonl`
files written by 5.0.x remain readable. New edge kinds (`jsx_render`,
`calls`, `references`) and the new `community` node attribute are
purely additions — graph consumers that ignore unknown attributes are
unaffected. No re-`gp init` is required to upgrade, though re-running
will surface the new edges and unblock the audit probes.

### Known limitations

- **`confidence_drift` audit probe still skips.** It looks for edges
  with `confidence == "INFERRED"` (string sentinel) and a numeric
  `confidence_score`, but the TS/JS and other adapters emit numeric
  confidence values (1.0, 0.7, …) directly. Unblocking requires
  tagging telemetry-overlay edges with the `INFERRED` sentinel — a
  separate piece of work, not a community-attribute issue.
- **`prop_type` candidates accumulate in prune output.** TypeScript
  `interface` and `type` declarations matching `*Props` / `*Properties`
  are flagged at confidence 0.4 because the adapter doesn't yet track
  type references as graph edges. A future `type_references` edge kind
  would close this.
- **Cross-file call resolution is not attempted.** Same-file scope
  resolution only. Cross-file calls remain in the unresolved-imports
  bucket and are graded by the `unresolved_imports` audit probe rather
  than rescuing dead-code candidates.
- **JSX `Foo.Bar = function() {}` property assignments aren't
  resolved.** The walker recurses into function bodies and handles
  named declarations, but does not capture sub-components attached to
  a parent via property assignment. These slip through as
  `jsx_internal` post-hoc rather than getting proper `jsx_render`
  edges.

## v4.1.0 — Resilience Foundation

Phase 11 (foundation block) of RESILIENCE_PLAN.md. Items 11.2 / 11.5 /
11.7–11.10 deferred to v4.2.0.

### New

- **Structured error contract** (`interface/errors.py`). Every
  user-facing error is now a `GraphifyError` subclass with stable
  `code`, one-line `message`, multi-line `remediation`, and structured
  `context`. The CLI wrapper renders code + message + remediation to
  stderr, dumps context to `<repo>/.graphify_plus/debug.log`, and exits
  with a per-code value. Codes wired now: `GP-CACHE-CORRUPT`,
  `GP-CACHE-VERSION-MISMATCH`, `GP-CACHE-LOCKED`, `GP-PARSE-TIMEOUT`,
  `GP-PARSE-MEMORY`, `GP-PARSE-UNSUPPORTED`, `GP-CONFIG-INVALID`,
  `GP-RESOURCE-EXHAUSTED`, `GP-INTEGRITY-CHECK-FAIL`,
  `GP-INTERNAL-ERROR`. `--debug` flag (or `GP_DEBUG=1`) enables full
  traceback to stderr in addition to the log.
- **Cache resilience** (`runtime/store.py`). `PRAGMA quick_check` on
  every open; corrupt files renamed to `cache.db.corrupt.<utc-ts>`
  before `GP-CACHE-CORRUPT` is raised. Schema versioning via the
  `meta` table — older or newer on-disk schemas raise
  `GP-CACHE-VERSION-MISMATCH`. Atomic writes via `BEGIN IMMEDIATE`
  with bounded exponential backoff (5 retries) before
  `GP-CACHE-LOCKED`. New `Store.backup()` writes
  `cache.db` → `cache.db.bak` via the SQLite backup API.
- **Per-file parse isolation** (`core/ingest.py`). Wall-clock timeout
  per worker (`GP_PARSE_TIMEOUT_SEC`, default 5) plus POSIX memory
  ceiling (`GP_PARSE_MEM_MB`, default 512). Worker death emits a stub
  symbol with `parse_status="failed"` and an entry in
  `<repo>/.graphify_plus/skipped.jsonl`. New `--strict` flag flips
  this to fail-fast (CI default). The orchestrator now never re-raises
  when a single file fails — `gp init` always succeeds end-to-end.
- **`gp doctor`** — single 'something feels off' diagnostic. Sections:
  System, Cache (size, schema version, sym/edge counts, quarantined
  files), Adapter coverage (skipped.jsonl summary), Recent errors
  (debug.log entry count). Exits 1 if any ERROR diagnostics are
  present. `--json` for machine-readable output.

### Fixed

- Inherited pre-Phase-0 `TestCentralityDrift::test_basic_run` failure
  closed permanently — verified deterministic across 5 consecutive
  runs (numpy + scipy already in deps from Phase 0).

### Deferred to v4.2.0

- 11.2 confidence propagation across every adapter
- 11.5 adapter regression corpus (30+ real-world files per language)
- 11.7 adversarial audit probes + trust score
- 11.8 opt-in telemetry + report-error
- 11.9 README/docs verification + versioned CLAUDE.md template
- 11.10 first-run wizard + `gp explain`
- All of Phase 12 (concurrency, watcher reconciliation, REST + web UI)

## v4.0.0 — Universal Context & Execution Engine

Phases 0–10 of EXECUTION_PLAN.md. Hardening (Phases 11–12) tracked
separately under RESILIENCE_PLAN.md.

### New

- **Tree-sitter universal frontend.** Adapters for Python, TypeScript,
  JavaScript, Go, Rust, Java (full) plus best-effort Ruby and C#. Single
  symbol-level data model — `id`, `kind`, `qualified_name`, `signature`,
  `path`, `span`, `exported`, `docstring`, `parent_id`, `language`.
- **Symbol-level graph layer** stored in a SQLite WAL cache at
  `<target_repo>/.graphify_plus/cache.db` plus a deterministic
  `graph_symbols.jsonl` artefact. Re-running `gp init` on an unchanged
  tree produces byte-identical output.
- **Content-addressed skeleton store (CAS).** Per-symbol skeletons keyed
  by `sha256[:16]`. Identical skeletons across files share one row.
- **Live AST watcher.** Polling-default on macOS, watchdog elsewhere;
  per-path debounced, single-transaction delta apply, subscriber API
  used by sync-docs and drift daemons.
- **Delta-Memory sessions** track which skeleton hashes have already
  been delivered to the AI, so subsequent context queries ship deltas
  instead of full re-sends.
- **Topological partitioning + token budgeter.** `gp context` packs the
  shortest entry→target path, 1-hop neighbourhood, and high-betweenness
  Gatekeepers into a tiktoken-counted budget; truncates to signatures
  when tight; emits a `« N additional nodes cut »` marker when over.
- **Semantic finder + ghost pruning.** Louvain communities with cached
  results, BM25 over qualified-name + signature + docstring weighted by
  community density. Heuristic dead-code, orphan and deprecated
  detection feeds an `--apply-prune` flag on `gp context`.
- **STAGING_PLAN manifest generator + sync-docs daemon.** `gp plan`
  derives an executable plan (topo-sorted impacted symbols, impact
  scores, validation checkpoints) and writes it into the **target
  repo's** root. Sync-docs daemon ticks rows as files change, 3-way
  merge preserves human-authored sections.
- **Unified constraint engine.** A single `runtime/rules.py` evaluator
  powers shadow simulation (`gp simulate`), pre-tool-use guardrails
  (`gp guardrails`), and continuous drift enforcement
  (`gp drift watch`). Declarative `rules.yaml`; `forbid_edge` and
  `forbid_cycle` rule kinds at v4.0.
- **Cross-boundary meta-graph & multi-agent orchestrator.** OpenAPI
  contract discovery, URL-literal cross-edges with confidence scoring;
  `gp coordinate` slices the graph by layer and emits one
  `STAGING_PLAN_<AGENT>.md` per agent in the target repo.
- **Enrichment layer.** `gp enrich git` (volatility / age / legacy
  flag), `gp enrich lockfile` (npm / pypi / poetry), `gp enrich
  telemetry <jsonl>` (privacy-redacted span schema fingerprints),
  `gp blast-radius <package>` (reverse-BFS along
  `depends_on / imports / calls`).
- **Privacy filter.** `interface/privacy.py` redacts emails, JWTs, API
  keys (sk-, xoxb-, ghp_, AKIA, AIza), long hex, IP addresses, phone
  numbers. Used by every external-data ingest path. Idempotent under
  hypothesis property tests.
- **Edge-centric test scaffolds + visual cluster renderer.**
  `gp matrix --generate-tests` emits per-edge templates in the relevant
  language. `gp visual --cluster N` produces a deterministic `.dot`
  always plus `.svg / .png` when `dot` is installed; output goes into
  `<target>/.graphify_plus/visuals/`.
- **MCP server + `gp claude-md`.** Run `python -m
  graphify_plus.interface.mcp_server` (requires the `[mcp]` extra) to
  expose every CLI command as an MCP tool. `gp claude-md` writes or
  refreshes a marker-delimited `## Graphify-Plus` section inside the
  **target repo's** CLAUDE.md, preserving human-authored content
  outside the markers.

### Changed

- Existing `audit` and `diff` CLIs continue to work unchanged at v3.x
  call sites.
- New package layout: `core/` (ingest), `runtime/` (cache, watcher,
  rules), `query/` (analysis), `interface/` (CLI + MCP). Top-level
  `graphify_plus/budget.py` re-exports the new `query.budget` module
  for backward compatibility.

### Operational invariant

Generated artefacts (`STAGING_PLAN.md`, `STAGING_PLAN_<AGENT>.md`,
`drift_report.md`, `cluster_<id>.svg`, `mcp.json`, `CLAUDE.md` section,
generated tests) are written into the **target repository's working
directory at runtime**. Graphify-Plus's own source tree never receives
runtime artefacts.

## v3.5.0 — 2026-05-03

Phase 5 release. STAGING_PLAN manifest generator + sync-docs daemon.

## v3.1.1 — pre-Phase-0

Last release before the v4 universal-engine refactor began. Provided
adversarial audit and graph-diff CLIs.
