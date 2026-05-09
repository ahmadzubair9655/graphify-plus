# graphify-plus public roadmap

Updated quarterly. ✅ ships in the current `feat/daemon-intent-tools` line; ⏳ next up; ❓ aspirational.

## Layer 1 — Latency parity with grep
- ✅ In-memory daemon over Unix socket
- ✅ Pre-computed indexes (label trie, by-path, 1-hop, PageRank, inverted text)
- ✅ Intent-typed tools (whats_in, who_calls, what_depends_on, find_by_name, find_by_concept)
- ✅ Token-budgeting by default

## Layer 2 — Staleness contract
- ✅ Watcher-driven incremental refresh
- ✅ Trust levels (FRESH / LIVE_AHEAD / STALE_FILES / STALE_REBUILD_NEEDED)
- ✅ Hybrid mode: graph + grep on stale paths

## Layer 3 — Routing
- ✅ `SKILL.md` install + pre-grep hook

## Layer 4 — Reinforcement
- ✅ Per-call receipts, telemetry sink, `gp daemon stats`
- ✅ Passive writeback proposals (`gp daemon writeback`)

## Layer 5 — Polish
- ✅ Source-grounded responses (file:line on every row)
- ✅ `gp daemon plan TASK`
- ✅ `gp daemon quickstart`
- ✅ Academic-name aliases (`whats_risky_to_change`, `whats_changed_since`)

## Layer 6 — Multi-source grounding
- ✅ GitHub issue/PR ingest, ADR ingest, `why_does_this_exist`
- ✅ Slack/Discord/Teams ingest (privacy allow-list)
- ✅ Conversation memory ingest (decision extraction)

## Layer 7 — Runtime intelligence
- ✅ Test coverage overlay (`whats_untested`)
- ✅ Runtime trace ingest (speedscope + pprof)
- ✅ Production error → code linkage (`find-origin`)
- ✅ Live profiler hook (py-spy attach)

## Layer 8 — Cross-stack edges
- ✅ HTTP boundary edges
- ✅ DB schema edges
- ✅ Infra-as-code edges (Terraform)
- ✅ Config + feature flag drift

## Layer 9 — Quality / security
- ✅ CVE overlay (`whats_vulnerable`)
- ✅ SAST overlay (`whats_risky`)
- ✅ License overlay + audit
- ✅ Architectural drift rules

## Layer 10 — Workflow products
- ✅ PR review co-pilot (`gp daemon review`)
- ✅ Onboarding mode (`gp daemon onboard`)
- ✅ Refactor playbooks (`gp daemon refactor`)
- ✅ Time-machine queries (`gp daemon time-machine`)
- ✅ Documentation generator (`gp daemon docs`)

## Layer 11 — Team and ecosystem
- ✅ Shared team graph (local-fs tier)
- ✅ Plugin architecture (entry points)
- ✅ Multi-repo monorepo support
- ✅ Skills marketplace (registry shim)

## Layer 12 — Operational rigor
- ✅ Privacy / security model (`gp daemon privacy --dry-run-network`)
- ✅ Performance contract (`gp daemon perfcheck`)
- ✅ Failure mode wrapper (`with_failover`)
- ✅ `gp daemon diagnose`
- ✅ Deterministic build hash

## Layer 13 — Flagship integration
- ✅ Always-on session (`gp daemon session-start|session-status`)
- ✅ Pre-edit ritual (`gp daemon pre-edit`)
- ✅ Post-session digest (`gp daemon session-digest`)

## Layer 14 — Correctness
- ✅ Provenance on every node/edge
- ✅ Hallucination prevention pipeline
- ✅ Ground-truth benchmark harness (`gp daemon benchmark`)
- ✅ Continuous correctness sampling

## Layer 15 — Scaling
- ✅ Hierarchical graphs (`build_coarse`)
- ✅ Sampled PageRank for huge graphs
- ✅ Approximation contracts (`Approximate.exact`)
- ✅ SQLite FTS5 fallback for cold paths
- ✅ Hyperscale (sharded daemon coordinator)

## Layer 16 — Query language
- ✅ GPL v1 (MATCH / FIND / WHERE / RETURN / COUNT BY)
- ✅ NL → query translator (rule-based fallback)
- ✅ Saved queries

## Layer 17 — IDE / editor
- ✅ LSP shim (hover / codeLens / definition)
- ✅ Inline annotations (LSP inlay hints)
- ✅ Status-bar pill (`graphifyPlus/statusBar`)
- ✅ Editor-native query (`graphifyPlus/runQuery`)

## Layer 18 — Embeddings
- ✅ Default-on, model-explicit (sentence-transformers when available)
- ✅ Embedding-augmented intent tools
- ✅ Reranking
- ✅ Stale embedding detection
- ✅ No external services required

## Layer 19 — Notifications
- ✅ Anomaly detection + trend + weekly digest
- ✅ Webhook / Slack / OS-notify outputs
- ✅ Quiet hours + rate limits

## Layer 20 — Recovery + audit
- ✅ Logical undo + tombstones
- ✅ Branch-namespaced annotations
- ✅ Full audit trail
- ✅ Deterministic log hash for rebuild verification

## Layer 21 — Schema versioning
- ✅ `_schema_version` on every artifact
- ✅ Forward migrations
- ✅ Backward migrations + lossy declaration

## Layer 22 — Local LLM / air-gapped
- ✅ Ollama / llama.cpp / MLX detection
- ✅ LLM-free mode
- ✅ Hybrid local/cloud routing
- ✅ Deterministic local extraction (seed)

## Layer 23 — CLAUDE.md / context files
- ✅ Owned section between markers
- ✅ Live content (god nodes / untested / rules / routing)
- ✅ Multi-tool support (CLAUDE.md / AGENTS.md / .cursorrules)
- ✅ Per-task hints (`--focus`)

## Layer 24 — Long-tail ingestors
- ✅ Jupyter notebooks
- ✅ OpenAPI / Swagger
- ✅ Postman collections
- ✅ Dockerfile / Compose
- ✅ Kubernetes manifests
- ✅ GitHub Actions
- ✅ i18n / translation files
- ✅ Asset references

## Layer 25 — Educational
- ✅ Built-in tutorial
- ✅ Recipes
- ✅ Reference manual (REFERENCE.md)

## Layer 26 — Multi-agent
- ✅ Read snapshots (MVCC)
- ✅ Per-target write coordination
- ✅ Per-agent token budgets
- ✅ Agent attribution (audit log `agent` field)

## Layer 27 — Distribution
- ✅ Version reporting + PyPI check
- ✅ Public roadmap (this file)
- ✅ One-line install spec (INSTALL.md)
- ✅ Telemetry consent (opt-in via env var)

## Layer 28 — Disqualifications
- ✅ Stated explicitly in README

## All layers shipped 🎉

Every Layer 1–28 item from the master plan is now shipped at the local-MVP tier. Items that need external infrastructure (Homebrew formula publication, multi-repo benchmark corpus, language-specific profiler adapters beyond py-spy) are noted in their respective module docstrings; they are extension surfaces rather than missing functionality.

The principle going forward: **fundamentals beat features over time**. Issues, not new layers, drive the next iteration.
