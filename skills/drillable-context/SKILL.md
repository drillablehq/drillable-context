---
name: drillable-context
description: >-
  Ground a claim about THIS project's own facts in the project's documented record instead of
  guessing. TRIGGER whenever you are about to state something specific to this codebase — a
  convention, a past decision, a config value, "we do X here", how a module behaves, a gotcha —
  that you would otherwise answer from assumption. Call context_search to find the record (or
  context_get a known slug); if it abstains ("no record"), say so rather than inventing the
  project's behavior. Standing instructions are in context_standing. Skip for general knowledge
  the project's own docs do not cover.
---

# Ground in the project's own facts, don't assume

This project's specific facts — its conventions, decisions, config, and gotchas — live in a grounded
corpus your agent can call (`context_search` / `context_get` / `context_standing`). When you're about
to assert something *about this codebase* that your priors would fill in, drill it instead:

- **A project-specific claim** ("we use X", "the build command is Y", "module Z returns W") →
  `context_search("<the question in your own words>")`, then answer from the cited record — or say
  "no record" if it abstains. Do NOT guess the project's behavior from how projects *usually* work;
  that is exactly the confident-wrong failure this prevents.
- **A standing instruction** (a preference that should hold every turn) is surfaced by
  `context_standing` — honor it.
- **An abstention is honest.** "No record" means the project hasn't documented it — surface that and
  ask, don't fabricate a plausible answer.
- **The boundary — name it or drill it.** If the fact lives in one place you can *point to*, open that
  file and read it. If it's *scattered* across the project's docs — a decision, a convention, a gotcha
  you'd fill in from how projects *usually* work — you don't know which file, so you're one step from
  guessing: that's the drill case. The assumption you'd otherwise make is precisely the confident-wrong
  answer this prevents.

The corpus grades a claim only by whether it still resolves to the source it cites — an anti-bluff
reflex over your own project's docs, not a general-knowledge oracle.
