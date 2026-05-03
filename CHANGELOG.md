# Changelog

All notable changes to graphify-plus.

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
