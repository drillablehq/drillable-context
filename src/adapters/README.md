# Source adapters — drill non-markdown corpora through the same engine

The engine indexes a folder of `*.md` (`seed.py`). An **adapter** converts some other corpus INTO that
markdown, so it rides the same retrieval stack (embed → `##`-section chunks → cosine floor/band → the MCP
verbs) with no engine change. Point a `configs/<name>.json` at the adapter's output and seed as usual.

## `sessions.py` — drill your own agent's session history

Converts Claude Code session transcripts (`~/.claude/projects/**/*.jsonl`) into markdown: one
`<sessionId>.md` per session, chunked by **turn** (one `## Turn N` section per user→assistant exchange — the
retrieval unit), with frontmatter `type: session` + `originSessionId:` so the engine grounds each as
**provenance** (a dated origin — the honest label; it's a record of what was said, cited to its transcript,
never "verified"). Thinking blocks are kept (capped) — they're the "what did the agent struggle with"
signal; tool *results* are dropped (retrieval noise), tool *uses* noted by name.

```
# 1. convert (a slice, or all)
python3 src/adapters/sessions.py --out ~/.drillable/sessions --project myrepo --since 2026-06-20
# 2. point a config at it (configs/sessions.json: facts_dir = the --out dir)
# 3. build + drill
python3 src/seed.py --config configs/sessions.json
#    → "how did we handle X", "what did agents struggle with", "when did I last touch Y" — grounded to the turn
```

**Dogfooded (first cut, 20 recent sessions → 360 turn-chunks):** "preserve session logs across three
accounts" → the exact turn that asked it (cosine 0.60, #1); "contested 100 km in miles fork" → the turn that
built it (0.48); "braid slate bank wrong path" → the right prior session (#1). Session history is usefully
retrievable.

### Agent-agnostic

Only `sessions.py` is Claude Code specific (the `.jsonl` format). The engine, the chunk shape, and the
`provenance` label are agent-neutral, so another agent (Cursor / Aider / Codex / Cline …) is a NEW adapter
emitting the same `.md` — a parser, not a rewrite.

### Not yet (follow-ups)

- **Project-scoped retrieval by default.** The store is machine-wide; scoping a query to the current
  project (cwd-derived slug + worktree slugs) so a user-level install doesn't contaminate one project's
  drills with another's needs a `project` column (from frontmatter) + a search filter (`--all` opt-in).
- Incremental re-convert (only new sessions); a `bin/` entry; per-agent adapters beyond Claude Code.
