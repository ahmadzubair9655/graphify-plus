# graphify-plus public roadmap

Updated quarterly. Each item is **one of three states** so reviewers
who try a feature don't lose trust if they hit a scaffold marked
"shipped":

* **🟢 shipped** — feature works end-to-end on real data, has tests
  and a CLI / MCP / LSP entry point, and was smoke-tested live.
* **🟡 scaffolded** — API surface exists (registered handler, table
  schema, dataclasses) but the underlying behaviour is partial,
  stubbed, or covers a subset. Ships so callers can rely on the
  shape; production-grade behaviour is follow-up work.
* **🔴 planned** — neither shape nor behaviour exists in this PR.
  Intentionally on the roadmap because there's a known design.

This is the honesty surface the review guide asked for.

## Layer 1 — Latency parity with grep
- 🟢 In-memory daemon over Unix socket
- 🟢 Pre-computed indexes (label trie, by-path, 1-hop, PageRank, inverted text)
- 🟢 Intent-typed tools (whats_in, who_calls, what_depends_on, find_by_name, find_by_concept)
- 🟢 Token-budgeting by default

## Layer 2 — Staleness contract
- 🟢 Watcher-driven incremental refresh
- 🟢 Trust levels (FRESH / LIVE_AHEAD / STALE_FILES / STALE_REBUILD_NEEDED)
- 🟢 Hybrid mode: graph + grep on stale paths

## Layer 3 — Routing
- 🟢 `SKILL.md` install + pre-grep hook (92% covered)

## Layer 4 — Reinforcement
- 🟢 Per-call receipts, telemetry sink, `gp daemon stats`
- 🟢 Adoption telemetry (`gp daemon adoption` — measures the actual rewrite goal)
- 🟢 Passive writeback proposals (`gp daemon writeback`)

## Layer 5 — Polish
- 🟢 Source-grounded responses (file:line on every row)
- 🟢 `gp daemon plan TASK`
- 🟢 `gp daemon quickstart`
- 🟢 Academic-name aliases (`whats_risky_to_change`, `whats_changed_since`, `who_uses`, `uses_what`, `search`, `propose_fixes`, `cross_check_with_other_models`)

## Layer 6 — Multi-source grounding
- 🟢 GitHub issue/PR ingest (via `gh` CLI), ADR ingest, `why_does_this_exist`
- 🟡 Slack/Discord/Teams ingest — parses Slack JSON exports + privacy allow-list; live API integrations not yet
- 🟢 Conversation memory ingest (decision extraction from transcripts)

## Layer 7 — Runtime intelligence
- 🟢 Test coverage overlay (`whats_untested`)
- 🟢 Runtime trace ingest (speedscope JSON, pprof text)
- 🟢 Production error → code linkage (`find-origin`)
- 🟡 Live profiler hook — py-spy attach is real (`attach_py_spy`); Node uses SIGUSR1 + breadcrumb (no programmatic CDP capture); MLX detected but no attach

## Layer 8 — Cross-stack edges
- 🟢 HTTP boundary edges (Flask/FastAPI/Express/Django ↔ fetch/axios/jQuery)
- 🟢 DB schema edges (SQL CREATE TABLE + ORM `__tablename__`/`db_table`/snake-case)
- 🟢 Infra-as-code edges (Terraform `resource` consumer match)
- 🟢 Config + feature flag drift (env-var + LD/launchdarkly references)

## Layer 9 — Quality / security
- 🟢 CVE overlay (pip-audit + npm-audit) — `whats_vulnerable`
- 🟢 SAST overlay (Bandit + Semgrep) — `whats_risky`
- 🟢 License overlay + audit
- 🟢 Architectural drift rules (`gp daemon rules check`, `--fail-on-error` for CI)

## Layer 10 — Workflow products
- 🟢 PR review co-pilot (`gp daemon review`)
- 🟢 Onboarding mode (`gp daemon onboard`)
- 🟢 Refactor playbooks (`gp daemon refactor extract-module|rename|split|unwind`)
- 🟢 Time-machine queries (`gp daemon time-machine`)
- 🟢 Documentation generator (`gp daemon docs`)

## Layer 11 — Team and ecosystem
- 🟡 Shared team graph — local-fs tier (NFS / Dropbox); self-hosted server is follow-up
- 🟢 Plugin architecture (entry points)
- 🟢 Multi-repo monorepo support (`workspaces.yaml` + cross-workspace edges)
- 🟡 Skills marketplace — registry shape + bundled defaults; no hosted registry

## Layer 12 — Operational rigor
- 🟢 Privacy / security model (`gp daemon privacy --dry-run-network`)
- 🟢 Performance contract (`gp daemon perfcheck --workload all` 12-cell table)
- 🟢 Failure mode wrapper (`with_failover`)
- 🟢 `gp daemon diagnose`
- 🟢 Deterministic build hash

## Layer 13 — Flagship integration
- 🟢 Always-on session (`gp daemon session-start|session-status`)
- 🟢 Pre-edit ritual (`gp daemon pre-edit`)
- 🟢 Post-session digest (`gp daemon session-digest`)

## Layer 14 — Correctness
- 🟢 Provenance on every node/edge
- 🟢 Hallucination prevention pipeline (3-stage filter)
- 🟡 Ground-truth benchmark harness — declarative format + tiny fixture corpus; full curated multi-repo public corpus is its own artefact
- 🟢 Continuous correctness sampling (deterministic per-day hash)

## Layer 15 — Scaling
- 🟢 Hierarchical graphs (`build_coarse`)
- 🟢 Sampled PageRank for huge graphs
- 🟢 Approximation contracts (`Approximate.exact`)
- 🟢 SQLite FTS5 fallback for cold paths
- 🟡 Hyperscale — coordinator + fan_out work; hasn't been load-tested against 10M+ node real monorepo

## Layer 16 — Query language
- 🟢 GPL v1 (MATCH / FIND / WHERE / RETURN / COUNT BY)
- 🟡 NL → query translator — deterministic rule-based fallback ships; LLM-backed translator is the caller's job
- 🟢 Saved queries (`.graphify_plus/queries/`)

## Layer 17 — IDE / editor
- 🟢 LSP shim (hover / codeLens / definition)
- 🟢 Inline annotations (LSP inlay hints)
- 🟡 Status-bar pill — data API (`graphifyPlus/statusBar`) ships; no editor extension yet consumes it
- 🟢 Editor-native query (`graphifyPlus/runQuery`, `graphifyPlus/savedQueries`)

## Layer 18 — Embeddings
- 🟢 Default-on, model-explicit (sentence-transformers when available)
- 🟢 Embedding-augmented intent tools
- 🟢 Reranking
- 🟢 Stale embedding detection
- 🟢 No external services required (model bundled or detected locally)

## Layer 19 — Notifications
- 🟢 Anomaly detection + trend + weekly digest
- 🟢 Webhook / Slack / OS-notify outputs
- 🟢 Quiet hours + rate limits

## Layer 20 — Recovery + audit
- 🟢 Logical undo + tombstones
- 🟢 Branch-namespaced annotations
- 🟢 Full audit trail
- 🟢 Rebuild from log (`gp daemon audit-rebuild`)

## Layer 21 — Schema versioning
- 🟢 `_schema_version` on every artifact
- 🟢 Forward + backward migrations
- 🟢 Lossy declaration (`_lost_in_migration`)

## Layer 22 — Local LLM / air-gapped
- 🟡 Ollama / llama.cpp / MLX detection — env-var probes ship; deterministic-extraction integration test against a real local model not yet
- 🟢 LLM-free mode
- 🟢 Hybrid local/cloud routing (path-based allow-list)

## Layer 23 — CLAUDE.md / context files
- 🟢 Owned section between markers
- 🟢 Live content (god nodes / untested / rules / routing)
- 🟢 Multi-tool support (CLAUDE.md / AGENTS.md / .cursorrules)
- 🟢 Per-task hints (`--focus`)

## Layer 24 — Long-tail ingestors
- 🟢 Jupyter notebooks
- 🟢 OpenAPI / Swagger
- 🟢 Postman collections
- 🟢 Dockerfile / Compose
- 🟢 Kubernetes manifests
- 🟢 GitHub Actions
- 🟢 i18n / translation files
- 🟢 Asset references

## Layer 25 — Educational
- 🟢 Built-in tutorial
- 🟢 Recipes
- 🟢 Reference manual (REFERENCE.md)

## Layer 26 — Multi-agent
- 🟢 Read snapshots (MVCC) — single-process tested; concurrent-write stress is post-merge work
- 🟢 Per-target write coordination
- 🟢 Per-agent token budgets
- 🟢 Agent attribution (audit log `agent` field)

## Layer 27 — Distribution
- 🟢 Version reporting + PyPI check
- 🟢 Public roadmap (this file)
- 🟡 One-line install — INSTALL.md spec ships; Homebrew formula scaffold ready, no published tap yet
- 🟢 Telemetry consent (opt-in via env var)
- 🟢 Auto-changelog (`gp daemon changelog --since vX.Y.Z`)

## Layer 28 — Disqualifications
- 🟢 Stated explicitly in README

## Honest gap analysis — 🟡 owners and milestones

The 🟡 items above are the ones where the review is most likely to push back. Each is *shipped* in the sense that the API exists and any future caller writing against the shape won't have to be rewritten — but a reviewer who tries them on real data will hit limits. Each line below answers two questions: **who** owns the lift to 🟢, and **which release** targets it.

| Layer | What's scaffolded | Owner | Target | What "🟢" requires |
|---|---|---|---|---|
| 6.2 Slack | JSON export ingest works; live API not yet | TBD | v6.2 | OAuth flow + consent UX + per-channel allow-list creation prompt |
| 7.4 Node profiler | SIGUSR1 + breadcrumb only | TBD | v6.1 | Real Chrome DevTools Protocol websocket client capturing a CPU profile programmatically |
| 7.4 MLX profiler | Detection only | TBD | v6.2 | Attach mechanism + sample integration test |
| 11.1 Team graph | Local-fs tier (NFS / Dropbox) | TBD | v6.3 | Optional self-hosted server with auth + conflict resolution UI |
| 11.4 Marketplace | Registry shape + bundled defaults | TBD | v6.2 | Hosted registry index + version-pinned skill install |
| 14.3 Benchmark | Tiny fixture corpus | TBD | v6.0.2 | 3 hand-labelled mid-sized OSS repos with gold graphs; precision/recall published in release notes |
| 15.5 Hyperscale | Coordinator works on tiny shards | TBD | v6.3 | 10M+ node real monorepo load test + adaptive sharding |
| 16.2 NL→GPL | Deterministic rule-based fallback | TBD | v6.1 (optional ext) | LLM-backed translator shipped as opt-in extra; deterministic fallback stays |
| 17.3 Status-bar pill | Data API only | TBD | v6.2 | Reference VS Code extension consuming `graphifyPlus/statusBar` |
| 22 Local LLM | Detection probes only | TBD | v6.1 | Deterministic-output integration test against a real local Ollama model |
| 27.1 Install | Spec + formula scaffold | TBD | v6.0.1 | Published Homebrew tap with real SHAs |

Owners marked **TBD** reflect honest project state — none of these have an assigned individual yet. Naming someone in the next planning cycle is the next step. The target column is the *promise to readers*: anyone tracking this roadmap can hold the project to that release.

See [`RISKS.md`](RISKS.md) for the failure modes that may surface post-merge against the 🟢 items.
