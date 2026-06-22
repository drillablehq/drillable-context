# drillable-context

*The engine that grounds a coding agent in **your own project's facts**.*

Your coding agent forgets. It contradicts a decision you made last week, invents a convention you
never set, "remembers" your API the way it wishes it worked. This points the agent at *your* facts —
your `CLAUDE.md`, your docs, your decisions and gotchas — and makes it **drill them instead of
guessing**, and say **"no record"** instead of bluffing when a fact isn't there.

It's the same engine proven twice inside the drillable project: on a repo's operating conventions,
and on an agent's own cross-session memory. This is that engine, pointed at any folder of markdown.

## The honest promise (read this before you trust it)

This is **anti-bluff, not a truth oracle.** It checks one thing: does a stated fact still resolve to
the source it cites? So it gives you:

- **your agent stops making up your conventions** — it answers from your docs or abstains;
- **a citation you can open** — every answer points at the file it came from;
- **graceful staleness** — if a doc moves or changes, the fact is flagged stale, never served as
  confidently wrong.

It does **not** tell you whether a fact is *correct* — only whether it's *what your docs say.* That's
a modest, cheap reflex, and it's deliberately all we claim.

## Install — point it at your facts

Works in any Claude Code (or any MCP client) — no plugin UI needed. **Zero-install with `npx`** (needs
Node and Python 3 — no clone, no pip):

```
claude mcp add drillable-context -- npx -y drillable-context --facts-dir /path/to/your/docs --name context
```

Or **from a clone**, if you'd rather skip npm:

```
claude mcp add drillable-context -- python3 /abs/path/src/server.py --facts-dir /path/to/your/docs --name context
```

Add `-s user` for all projects; `-e DRILLABLE_EMBED=true` for semantic retrieval (uses your
`OPENAI_API_KEY`). Restart and your agent gets `context_search` / `context_get` / `context_standing` /
`context_stats`; the index builds itself on the first call, and the running server rebuilds it when you
edit or add a fact — the next query reflects the change (no restart, no manual reseed).

**One-click, if your client has the plugin UI** (`/plugin`) or the `claude plugin` CLI:

```
/plugin marketplace add drillablehq/drillable-context   # or a local path
/plugin install drillable-context@drillable
```

That same `drillable` marketplace also carries **use-drillable** (makes your agent reach for cited
*reference* facts — the MCP spec, units, networking, …) — add it too with
`/plugin install use-drillable@drillable`. It's the same marketplace whichever repo you add it from.
All the developer tools are at [drillable.com/dev](https://drillable.com/dev).

It prompts for your facts directory at enable (the `userConfig`) — nothing to type. Ships **disabled**:
it reads your files (and, if you opt into semantic retrieval, sends their text to OpenAI to embed), so
enabling it is your call.

## How it works (under the hood)

Whatever the install path, it's the same engine: point it at a folder of markdown, build an index,
serve it over MCP. To drive it from a config file (and customize the split / oracle / embeddings):

1. **Point it at your facts.** A `config.json`:
   ```json
   {
     "name": "myproject",
     "facts_dir": "/path/to/your/docs",      // any folder of *.md
     "oracle_repo": "/path/to/your/repo",    // optional — re-check file/PR references here
     "standing_types": ["preference"],        // frontmatter types that stay always-loaded
     "recursive": true,
     "embed": true                            // semantic retrieval (needs OPENAI_API_KEY); omit → keyword
   }
   ```
2. **Build the index** (the `.md` stay the source of truth; the DB is rebuilt every run):
   ```
   python3 src/seed.py --config configs/myproject.json
   ```
3. **Connect it to your agent** (any MCP client — Claude Code, Cursor, …):
   ```
   claude mcp add myproject -- python3 /abs/path/src/server.py --config /abs/path/configs/myproject.json
   ```
   Your agent gets `myproject_search`, `myproject_get`, `myproject_standing`, `myproject_stats`.

## The split — what's always-on vs fetched

Two kinds of facts behave differently, so they're served differently:

- **standing** — instructions that must apply *every* turn ("we use tabs", "never touch the billing
  module"). Always loaded.
- **queryable** — the large reference tail (decisions, gotchas, status). Fetched on demand, so it
  scales without bloating the agent's context.

A fact is **standing** if its frontmatter `type` is in your `standing_types`; everything else is
queryable.

## What grounds a fact (and what doesn't)

Each fact is labelled honestly:

- **cited** — it names a file or PR that still resolves → it drills to that source.
- **provenance** — it records where/when it was decided, but the original record may be gone → dated,
  not live-checkable.
- **judgment** — a preference with no external source. Stored and served, **never** labelled
  "verified." (Grading a preference against itself would be circular.)

## Frontmatter conventions

Facts are just markdown. An **optional** YAML frontmatter block tells the engine how to file each one —
every key is optional, and a plain `.md` with no frontmatter still indexes fine.

```markdown
---
type: preference              # the split: in your standing_types → standing; otherwise queryable
originSessionId: 6f1e9c20     # provenance: the session it was decided in (the log may be long gone)
description: Tabs, not spaces # title fallback when the body has no "# heading"
---
We indent with tabs, never spaces — see src/format.py.
```

- **`type:`** (or whatever key you set as `type_field`) drives **the split**. A fact whose `type` is one
  of your `standing_types` (e.g. `preference`) is **standing** — always loaded, and grounded **judgment**
  (a preference isn't graded against a source). Every other `type` is **queryable**, fetched on demand.
- **`originSessionId:`** drives **provenance**. A queryable fact that names no file or PR but records
  where it was decided is grounded **provenance** (dated, not live-checkable) rather than bare judgment.
- Naming a **file path or `#PR`** anywhere — body or frontmatter — makes a queryable fact **cited**: it
  drills to that source. This one needs no frontmatter at all.

**Plain markdown works.** Point it at a bare `CLAUDE.md` or a `docs/` tree with no frontmatter and every
fact is queryable — grounded **cited** where it names files, **judgment** otherwise. You just don't get
the standing/queryable split or dated provenance until you add the keys; the grounding ladder stays flat
by design, not by failure.

## Privacy

Everything is local: your facts never leave your machine, the server is a local subprocess (stdio,
no network), and the index is a file in this folder. The eventual paid tier is *more* privacy
(no-log, self-host), not less.

It also **follows your repo's `.gitignore`** (when the facts dir is a git repo): ignored files —
build artifacts, vendored deps, and **gitignored secrets** like `.env` or keys — are never indexed or
sent to OpenAI. *Honest scope:* this skips *ignored* files (the common case for secrets) — it is **not**
a secret-scanner; a secret that's committed or sits inside a *tracked* file would still be indexed.
So you can point at a repo root without dragging in build junk or your ignored secrets — but don't
treat it as a guarantee that no secret can ever reach the index.

## Notes

- **Stdlib only** — no pip install; Python 3. Embeddings call OpenAI over `urllib` (an `OPENAI_API_KEY`,
  not a dependency); without one, retrieval falls back to the keyword scorer automatically.
- **`npx` is just a launcher.** The npm package is a tiny zero-dependency Node shim that spawns the
  bundled Python server (`src/server.py`) with your args and inherits its stdio — it adds no npm runtime
  deps and no Python packages. Set `DRILLABLE_PYTHON` to choose the interpreter.
- **Retrieval scales.** Keyword is fine for a small corpus or when you query in the docs' own words; for
  a real repo, set `"embed": true` — semantic retrieval gets ~94% recall@3 vs ~67% for keyword (and
  100% on natural-language questions). An off-topic query still returns "no record" (a cosine floor).
- **MCP**: the server speaks stdio JSON-RPC and negotiates the client's protocol version (verified
  spec-correct against the MCP reference as of revision 2025-11-25). The 2026-07-28 MCP release
  candidate removes the `initialize` handshake; this server will need a small update then, and keeps
  working in the meantime via backward compatibility.
- **Status: prototype, validated.** A bluff-rate eval passed (grounding cut confident-wrong answers
  29% → 0% and 43% → 100% correct on facts the agent couldn't know), and retrieval scales (above).
  Still ahead: a real external user.
