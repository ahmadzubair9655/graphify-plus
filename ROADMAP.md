# graphify-plus public roadmap

Updated quarterly. Items marked ✅ ship in the current `feat/daemon-intent-tools` line; ⏳ are next-up; ❓ are aspirational and not yet committed.

## Layer 1 — Latency parity with grep
- ✅ In-memory daemon over Unix socket
- ✅ Pre-computed indexes (label trie, by-path, 1-hop, PageRank, inverted text)
- ✅ Intent-typed tools (whats_in, who_calls, what_depends_on, find_by_name, find_by_concept)
- ✅ Token-budgeting by default

## Layer 2 — Staleness contract
- ✅ Watcher-driven incremental refresh
- ✅ Trust levels (FRESH / LIVE_AHEAD / STALE_FILES / STALE_REBUILD_NEEDED)
- ⏳ Hybrid mode: graph + grep on stale paths

## Layer 3 — Routing
- ✅ `SKILL.md` install + pre-grep hook

## Layer 4 — Reinforcement
- ✅ Per-call receipts, telemetry sink, `gp daemon stats`
- ⏳ Passive writeback proposals

## Layer 5 — Polish
- ✅ Source-grounded responses (file:line on every row)
- ✅ `gp daemon plan TASK`
- ⏳ `gp quickstart`
- ⏳ Drop academic naming externally

## Layer 6 — Multi-source grounding
- ✅ GitHub issue/PR ingest, ADR ingest, `why_does_this_exist`
- ⏳ Slack/Discord/Teams ingest (privacy-sensitive)
- ⏳ Conversation memory hook

## Layer 7 — Runtime intelligence
- ✅ Test coverage overlay (`whats_untested`)
- ⏳ Runtime trace ingest (py-spy / pprof)
- ⏳ Production error → code linkage
- ❓ Live profiler hook

## Layer 8 — Cross-stack edges
- ✅ HTTP boundary edges
- ✅ DB schema edges
- ⏳ Infra-as-code edges (Terraform / K8s)
- ⏳ Config + feature flag edges

## Layer 9 — Quality / security
- ✅ CVE overlay (`whats_vulnerable`)
- ✅ SAST overlay (`whats_risky`)
- ⏳ License overlay
- ✅ Architectural drift rules

## Layer 10 — Workflow products
- ✅ PR review co-pilot (`gp daemon review`)
- ✅ Onboarding mode (`gp daemon onboard`)
- ⏳ Refactor playbooks
- ⏳ Time-machine queries
- ⏳ Documentation generator

## Layer 11 — Team and ecosystem
- ⏳ Shared team graph
- ✅ Plugin architecture (entry points)
- ⏳ Multi-repo monorepo support
- ❓ Public skills marketplace

## Layer 12 — Operational rigor
- ⏳ Privacy / security model declared
- ⏳ Performance contract per machine
- ⏳ Failure modes
- ✅ `gp daemon diagnose`
- ⏳ Deterministic builds

## Layer 13 — Flagship integration
- ❓ Always-on session
- ❓ Pre-edit ritual
- ✅ Post-session digest

## Layer 14 — Correctness
- ✅ Provenance on every node/edge
- ✅ Hallucination prevention pipeline
- ❓ Ground-truth benchmark harness
- ❓ Continuous correctness sampling

## Layer 15 — Scaling
- ⏳ Hierarchical graphs
- ⏳ Incremental algorithms
- ⏳ On-disk indexes for cold paths
- ⏳ Sampling / approximation contracts
- ❓ Hyperscale mode

## Layer 16 — Query language
- ✅ GPL v1
- ✅ NL → query translator (rule-based fallback)
- ✅ Saved queries

## Layer 17 — IDE / editor
- ✅ LSP shim (hover / codeLens / definition / status-bar)
- ⏳ Inline annotations
- ⏳ Status-bar pill (data is in `graphifyPlus/statusBar`; UI work is editor-side)
- ⏳ Editor-native query

## Layer 18 — Embeddings
- ✅ Default-on, model-explicit
- ✅ Reranking
- ✅ Stale embedding detection
- ✅ No external services required

## Layer 19 — Notifications
- ✅ Anomaly detection + trend + weekly digest
- ⏳ Webhook / Slack / email outputs

## Layer 20 — Recovery + audit
- ✅ Logical undo + tombstones
- ✅ Branch-namespaced annotations
- ✅ Full audit trail
- ⏳ Rebuild from log

## Layer 21 — Schema versioning
- ✅ `_schema_version` on every artifact
- ✅ Forward migrations
- ⏳ Backward migrations + lossy declaration

## Layer 22 — Local LLM / air-gapped
- ✅ Ollama / llama.cpp / MLX detection
- ✅ LLM-free mode
- ✅ Hybrid local/cloud routing

## Layer 23 — CLAUDE.md / context files
- ✅ Owned section between markers
- ✅ Live content
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
- ⏳ Reference manual

## Layer 26 — Multi-agent
- ⏳ Read snapshots (MVCC)
- ⏳ Write coordination
- ⏳ Agent attribution
- ⏳ Subagent budgets

## Layer 27 — Distribution
- ✅ Version reporting
- ✅ Public roadmap
- ⏳ One-line install across platforms
- ⏳ Telemetry consent UX

## Layer 28 — Disqualifications
- ✅ Stated explicitly in README

## Sequencing principle

**Fundamentals beat features over time.** Every ⏳ in Layers 12–14 / 21 takes precedence over new ✅ items in Layers 24–25 if forced to choose.
