---
name: graphify-plus
description: Routing rule — when to use graphify-plus intent tools instead of grep/glob/read. Trigger automatically on questions about call graphs, dependencies, blast radius, "where does X live", "what depends on Y", "what's central", or while navigating an unfamiliar codebase.
---

# graphify-plus routing

The graphify-plus daemon answers structural questions about this codebase
in sub-millisecond P50, with file:line grounding. It is *not* a replacement
for grep — it complements grep on questions grep can't answer cheaply.

## When to prefer graphify-plus

Use the **graph** when the question is about:

- **why** something exists or was added
- **what depends on / what is depended on by** something
- **what changed** between two states (commits, branches)
- **what is most central** to the codebase (PageRank)
- **what would break** if X is removed (blast radius)
- navigating an **unfamiliar codebase** for the first time

Specific tool routing:

| Question                                       | Tool                            |
| ---------------------------------------------- | ------------------------------- |
| "What's in this file/module?"                  | `gp_whats_in`                   |
| "Who calls this function?"                     | `gp_who_calls`                  |
| "What does this function call?"                | `gp_whos_called_by`             |
| "What depends on this symbol?"                 | `gp_what_depends_on`            |
| "What does this symbol depend on?"             | `gp_what_does_this_depend_on`   |
| "Find a symbol named like X"                   | `gp_find_by_name`               |
| "Find the code that handles X (concept)"       | `gp_find_by_concept`            |
| "What are the most central symbols?"           | `gp_whats_central`              |

Each tool returns rows with `{node_id, label, source_file, line_number,
snippet, confidence}` so the result pipes straight into Read/Edit
without a second round-trip.

## When to prefer grep

Use **grep** when:

- you have a literal string and want **exact matches** (including
  comments, tests, vendored code)
- you want **every occurrence** of a token regardless of structure
- the file you suspect has the answer is small enough that reading it is
  one tool call
- the graph reports `STALE_REBUILD_NEEDED` and you can't or shouldn't
  rebuild right now

## Default rule

> Graph first for **structural** questions, grep first for **literal**
> questions, and never both. If unsure, ask graphify-plus and read the
> freshness envelope on the response — `trust=FRESH` means the answer
> reflects the working tree.

## Reading the freshness envelope

Every response includes:

```json
{
  "freshness": {
    "trust": "FRESH | LIVE_AHEAD | STALE_FILES | STALE_REBUILD_NEEDED",
    "freshness_token": "12-hex hash that changes when the graph changes",
    "files_changed_since": 0,
    "stale_paths": ["…", "…"],
    "hint": "human-readable guidance when trust degrades"
  }
}
```

When `trust = STALE_FILES`, the hint will name the affected paths;
prefer grep on those specific files for the duration of that staleness
window. When `trust = FRESH`, trust the structural answer.

## Reading the receipt

Every response also carries a one-line receipt:

```
[graphify-plus] who_calls · 0.05ms · 38 tokens · would have taken
~3 grep calls + 1 file reads
```

The receipt is your in-context confirmation that using the graph saved
work. If you see receipts trending toward zero saved calls, the question
class probably wasn't graph-shaped — fall back to grep.

## Daemon liveness

If a tool returns `DAEMON_NOT_RUNNING`, the user can start it with::

    gp daemon start --detach

Or every intent tool will fall back to a slower in-process build when
the daemon isn't running, so you can still run the query — just slower.
