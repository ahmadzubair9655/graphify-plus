# Changelog

All notable changes to graphify-plus.

## Unreleased

### New

- **JSX-internal symbol tracing** (`core/adapters/_ts_common.py`). The TS/JS
  adapter now does a second pass over `.tsx`/`.jsx` files (and any `.ts`/`.js`
  file containing JSX), resolving `<Component />` references to function or
  component declarations in the same file. Resolved references are emitted as
  `jsx_render` edges, which `gp prune` counts as inbound liveness evidence.
  The walker also descends into function bodies so nested local components
  become symbols. Patterns covered: simple identifier (`<Foo/>`), member
  expression (`<Card.Header/>`), arrow-function components, and components
  declared inside another component's body. Host elements (`<div/>`,
  `<svg/>`) are filtered out.

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
