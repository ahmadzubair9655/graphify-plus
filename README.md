# graphify-plus

> Drop-in enhancement layer for [safishamsi/graphify](https://github.com/safishamsi/graphify).  
> Point it at your existing `graphify-out/graph.json` and it runs. No changes to graphify required.

```bash
pip install graphify-plus
graphify-plus ./graphify-out/graph.json
```

---

## What graphify-plus adds

graphify already does the hard part — it turns any folder of code, docs, papers, images, and videos into a queryable knowledge graph in one command. graphify-plus runs after that and makes the graph smarter, more trustworthy, and more useful to query.

| Module | What it does |
|---|---|
| `temporal` | Enriches every node with git history — `first_seen`, `last_modified`, `commit_author`. Detects `supersedes` edges from rename/refactor commits. One bulk `git log` call regardless of corpus size. |
| `reconcile` | After `graphify merge-graphs`, finds semantically identical nodes from different repos and adds `cross_repo_equivalent_to` edges. Auto-detects namespaces from file paths — no manual tagging required. |
| `causal` | Asks "what forced this node to exist?" via a targeted LLM pass. Adds `caused_by / mandated_by / required_by / response_to / evolved_from` edges. Targets nodes intelligently (security/patch/fix/deprecated keywords, rationale_for neighbours, recently-added nodes) rather than just high-degree nodes. |
| `contradict` | Detects contradictions between AST-extracted and LLM-inferred edges. Builds exclusive pairs dynamically from your graph's actual relation types, not a hardcoded list. Also flags docstring/implementation conflicts (e.g. "idempotent" in docstring but write edges in AST). |
| `budget` | Replaces blunt token-count truncation with importance-ranked subgraph extraction. Nodes scored by degree centrality, hop distance, edge confidence, and temporal recency. Fuzzy label+id matching so hash-format node IDs don't break queries. Module-level centrality cache. |
| `writeback` | Adds write tools to the graphify MCP server: `annotate_node`, `correct_edge`, `flag_ambiguous`. Corrections go to an append-only `corrections.jsonl` ledger with idempotency guards and conflict detection. `apply_corrections` merges them into `graph.json` at rebuild time. |
| `report_json` | Produces a machine-readable `report.json` alongside `GRAPH_REPORT.md`. Includes a 0–100 **health score** (trust ratio, contradiction count, temporal coverage, causal coverage, ambiguity flags). Structured warnings from every module. PreToolUse hook injection helper. |

After the pipeline runs, the fully-enriched graph is saved to `graphify-out/graph_enhanced.json` — all new node attributes and edge types preserved, nothing evaporates.

---

## Install

```bash
pip install graphify-plus          # enhancement layer only
pip install "graphify-plus[full]"  # includes graphifyy (the original graphify)
```

Requires Python 3.10+. The only core dependency is `networkx>=3.0`.

---

## Quickstart

**Option A — Enhance an existing graph (one command):**

```bash
graphify-plus ./graphify-out/graph.json
```

Writes:
- `graphify-out/report.json` — structured machine-readable report
- `graphify-out/graph_enhanced.json` — enriched graph with all new nodes and edges
- Appends contradiction and causal chain sections to `GRAPH_REPORT.md`

**Option B — Python API:**

```python
from graphify_plus import enhance_existing_graph

result = enhance_existing_graph(
    graph_json_path="./graphify-out/graph.json",
    corpus_root="./my-repo",   # for git temporal enrichment
    verbose=True,
)

print(result["report"]["health"]["summary"])
# → "Health 81/100 (B) — main drag: 3 contradictions"
```

**Option C — After graphify's build_graph() + cluster() in your own pipeline:**

```python
from graphify_plus import run_enhanced_pipeline

# G is the nx.Graph from graphify's pipeline
result = run_enhanced_pipeline(
    G,
    out_dir=Path("graphify-out"),
    corpus_root=Path("."),
    run_causal=True,      # opt-in LLM pass
    llm_fn=my_llm_call,   # callable(system_prompt, user_prompt) -> str
)
```

**Option D — Stats only (no re-run):**

```bash
graphify-plus ./graphify-out/graph.json --stats-only
```

```
📊 graphify-plus stats
────────────────────────────────────────────
  🟡 Health score: 74/100  (grade B)
     Health 74/100 (B) — main drag: low trust ratio (38% EXTRACTED)
────────────────────────────────────────────
  Nodes:          312  (undirected)
  Edges:          891
  Communities:     14
  Git nodes:      287

🌟 Top god nodes:
  [ 47°] AuthService
  [ 39°] UserModel
  [ 31°] RequestRouter
  [ 28°] DatabasePool ⚠️
  [ 22°] TokenValidator

🏷  Confidence: EXTRACTED=412 INFERRED=467 AMBIGUOUS=12

⚠️  Contradictions: 4 (pairs checked: 23)

🔗 Causal chains: 3
  CVE-2023-4521 → 6 downstream
  OAuth2Migration → 4 downstream
  DeprecatedAuthV1 → 2 downstream

❓ Suggested questions:
  • What is the relationship between AuthService and TokenValidator?
  • Why does DatabasePool have contradicting edges?
  • What triggered the OAuth2 migration?
```

**Option E — Apply accumulated session corrections:**

```bash
graphify-plus ./graphify-out/graph.json --apply-corrections
```

---

## MCP write-back

Add these tools to your graphify MCP server to let the AI assistant improve the graph during sessions:

```python
from graphify_plus.writeback import handle_write_tool, apply_corrections, WRITE_TOOL_SCHEMAS

# In your serve.py tool dispatch — add alongside existing read tools:
if tool_name in ("annotate_node", "correct_edge", "flag_ambiguous"):
    return handle_write_tool(tool_name, tool_input, G, corrections_path)
```

The three write tools:

- **`annotate_node(node_id, note)`** — attach a note to any node. Idempotent: same note twice = one record.
- **`correct_edge(source_id, target_id, new_relation)`** — fix a wrong relation type. Conflict-safe: warns if a previous correction disagrees.
- **`flag_ambiguous(node_id, reason)`** — mark a node for human review.

All corrections go to `corrections.jsonl` (append-only, auditable). At rebuild time:

```bash
graphify-plus ./graphify-out/graph.json --apply-corrections
# ✓ Applied 7 corrections to graphify-out/graph.json
# Skipped 2 (already applied)
```

---

## PreToolUse hook integration

Replace the existing `GRAPH_REPORT.md` text injection with structured JSON from `report.json`. More signal per token, directly queryable by the assistant:

```python
from graphify_plus.report_json import format_hook_injection
injection = format_hook_injection(Path("graphify-out/report.json"), token_budget=600)
```

Injects a compact block including health score, top god nodes, community count, contradiction count, and suggested questions — all machine-readable.

---

## Cross-repo reconciliation

After `graphify merge-graphs repo1/graphify-out/graph.json repo2/graphify-out/graph.json`:

```python
from graphify_plus.reconcile import reconcile_merged_graph

# Namespaces are auto-detected from source_file path topology
result = reconcile_merged_graph(G)
# → {"edges_added": 12, "namespace_summary": {"repo-A": 180, "repo-B": 210}}

# Or with LLM for higher accuracy:
result = reconcile_merged_graph(G, llm_fn=my_llm_call, confidence_threshold=0.75)
```

Nodes from different repos that turn out to be the same concept get `cross_repo_equivalent_to` edges, making cross-repo queries coherent instead of fragmented.

---

## Budget-aware querying

```python
from graphify_plus.budget import extract_budgeted_subgraph, find_root_ids_for_query

# Works even with hash-format node IDs
roots = find_root_ids_for_query(G, "auth flow")  # fuzzy match: exact → partial → edit-distance

context = extract_budgeted_subgraph(G, root_ids=roots, token_budget=1500)
# Returns ranked subgraph string, ready to inject into LLM prompt
# Nodes scored by: centrality (40%) + hop distance (25%) + confidence (20%) + recency (15%)
# Overflow nodes get a placeholder: "... 14 additional nodes cut by budget"
```

---

## Health score

The health score gives you a single headline number for graph quality. Computed from five weighted components:

| Component | Weight | Signal |
|---|---|---|
| Trust ratio (EXTRACTED / total edges) | 30 pts | How much came from deterministic AST vs LLM inference |
| Contradiction penalty | 25 pts | Number of detected contradictions |
| Temporal coverage | 20 pts | Fraction of nodes with git history |
| Causal coverage | 15 pts | Fraction of god nodes with causal edges |
| Ambiguity penalty | 10 pts | Number of flagged-ambiguous nodes |

```
Health 81/100 (B) — main drag: 3 contradictions
Health 94/100 (A)
Health 47/100 (D) — main drags: low trust ratio (31% EXTRACTED), 11 contradictions
```

---

## Warnings

Every module failure is recorded as a structured warning instead of silently swallowed:

```json
"warnings": [
  {"module": "temporal", "op": "enrich_graph_with_temporal", "reason": "Permission denied: .git/"},
  {"module": "causal", "op": "extract_causal_chains", "reason": "LLM call timed out"}
]
```

Warnings appear in `report.json`, in the terminal stats output, and in the PreToolUse hook injection. You always know which passes ran successfully and which didn't.

---

## Directed graph support

graphify supports `--directed` mode (preserves edge direction). graphify-plus respects it:

- `budget.py` uses `successors/predecessors` BFS for directed graphs
- `contradict.py` maintains separate `(a→b)` and `(b→a)` edge pairs
- `causal.py` traverses upstream via `predecessors` for context gathering
- `graph_enhanced.json` records the `_directed` flag so it round-trips correctly

---

## Architecture

All modules are independent. They communicate through the same plain NetworkX graph that graphify uses — no new databases, no servers, no side effects outside `graphify-out/`.

```
graphify pipeline  (detect → extract → build_graph → cluster → analyze → report → export)
        │
        ▼
[graphify-plus enhancement pipeline]
        │
        ├─ enrich_graph_with_temporal()    one bulk git log, O(1) lookups
        ├─ detect_contradictions()         dynamic exclusive pairs from your graph
        ├─ extract_causal_chains()         interest-scored targets, not raw degree
        ├─ reconcile_merged_graph()        auto-namespace from file paths
        │
        ├─ [queries] extract_budgeted_subgraph()   cached centrality, fuzzy matching
        ├─ [MCP]     handle_write_tool()           idempotent corrections ledger
        │
        └─ build_report_json()             health score + warnings + structured data
                │
                ├─ graphify-out/report.json
                ├─ graphify-out/graph_enhanced.json   ← enriched graph persisted
                └─ graphify-out/GRAPH_REPORT.md       ← contradiction + causal sections appended
```

---

## Tests

```bash
pytest tests/ -v   # 39 tests, all passing
```

Tests cover all 7 enhancement modules plus all 9 correctness fixes (directed graph awareness, bulk git log, fuzzy query matching, dynamic contradiction pairs, smart causal target selection, writeback idempotency, health score, structured warnings, graph export).

---

## Relationship to graphify

graphify-plus is a fan project. It is not affiliated with or endorsed by [safishamsi](https://github.com/safishamsi). It depends on `graphifyy` (the official PyPI package) and is designed to be forward-compatible with graphify's graph.json schema.

If graphify ships any of these features natively, the corresponding graphify-plus module becomes redundant and can be dropped.

---

## License

MIT
