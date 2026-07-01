#!/usr/bin/env python3
"""server.py — serve a private-facts corpus over stdio MCP (config-driven, self-contained).

Newline-delimited JSON-RPC 2.0 on stdin/stdout (initialize / tools/list / tools/call) — spec-correct
per the MCP `mcp` corpus: stdout carries ONLY MCP messages (the seed bootstrap is redirected to
stderr). Stdlib only; no framework dependency. Verbs are name-prefixed (`<name>_search/get/standing/
stats`) so several corpora can mount without colliding.

  python3 src/server.py --config configs/<name>.json
"""
import argparse
import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import embed  # noqa: E402 — optional embedding retriever; keyword fallback when no key
import config  # noqa: E402 — resolves a corpus config from a file or params/env

STOP = set("the a an of to in is are for and or it its on at as be by we you i with not this that "
           "these those how what why when who do does".split())
MARK = {"cited": "⛓ cited", "provenance": "◷ provenance", "judgment": "· judgment"}


def _pkg_version(default="0"):
    """package.json version — the single source of truth, so serverInfo can't drift from the release."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "package.json")) as f:
            return json.load(f).get("version", default)
    except Exception:  # noqa: BLE001 — version display is best-effort; never fail the server over it
        return default


VERSION = _pkg_version()


def load_cfg(argv):
    return config.resolve(argv)


def facts_mtime(cfg):
    """Newest mtime among the facts dir's *.md files — the freshness signal for reindex-on-change.
    Catches in-place edits and new files (a re-seed rebuilds the whole corpus from the current files,
    so renames are covered too); a pure delete is the one case left for a manual reseed/restart.
    Tracking *.md files only — not directory mtimes — keeps editor swap-file churn, and our own DB
    write when the DB lives inside the facts dir, from forcing spurious rebuilds. Skips dot-dirs (no
    .git walk); returns 0.0 if the dir is gone, so a vanished facts_dir keeps the last good DB."""
    root = cfg["facts_dir"]
    if not os.path.isdir(root):
        return 0.0
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if not cfg.get("recursive", True):
            dirnames[:] = []
        for f in filenames:
            if f.endswith(".md"):
                with contextlib.suppress(OSError):
                    newest = max(newest, os.stat(os.path.join(dirpath, f)).st_mtime)
    return newest


def _db_schema_version(db):
    try:
        c = sqlite3.connect(db)
        v = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        return v
    except Exception:
        return None


AUTO_FETCH_THROTTLE = int(os.environ.get("DRILLABLE_AUTO_FETCH_THROTTLE", "600"))  # min seconds between auto-fetches


def _auto_advance(cfg):
    """If cfg['auto_refresh'] names a ref (e.g. 'origin/main'), keep a CLEAN dedicated worktree pinned to
    it: a throttled fetch, and ONLY when the working tree is clean, fast-forward (detach) to the ref and
    signal a reseed. Returns True iff it advanced (the caller then reseeds).

    This is the freshness mechanism the pinned-worktree topology actually needs: `_facts_behind`'s warning
    keys on `@{upstream}`, which a DETACHED worktree (the recommended setup) doesn't have — so the warning
    never fires there. Safe-by-default: a DIRTY tree, a missing ref, no git, or any error → returns False
    and nothing is touched (the clobber guard — never auto-advance a tree with local edits is the whole
    safety argument; that case keeps the surface-only behaviour). Throttled so the query hot path fetches
    at most once per AUTO_FETCH_THROTTLE seconds."""
    ref = cfg.get("auto_refresh")
    if not ref or not isinstance(ref, str):
        return False
    fd = cfg["facts_dir"]
    marker = cfg["_db"] + ".autofetch"
    try:
        if os.path.exists(marker) and (time.time() - os.path.getmtime(marker)) < AUTO_FETCH_THROTTLE:
            return False
    except Exception:
        pass
    try:                                   # throttle regardless of outcome (touch BEFORE the network call)
        open(marker, "a").close()
        os.utime(marker, None)
    except Exception:
        pass
    try:
        def git(*a, t=8):
            return subprocess.run(["git", "-C", fd, *a], capture_output=True, text=True, timeout=t)
        # SAFETY: never advance a tree with local edits — the only case a checkout could clobber work.
        st = git("status", "--porcelain", t=5)
        if st.returncode != 0 or st.stdout.strip():
            return False                   # not a git repo, or dirty → surface-only
        remote = ref.split("/", 1)[0]
        branch = ref.split("/", 1)[1] if "/" in ref else "HEAD"
        if git("fetch", "--quiet", remote, branch, t=25).returncode != 0:
            return False
        head = git("rev-parse", "HEAD", t=5).stdout.strip()
        tgt = git("rev-parse", ref, t=5).stdout.strip()
        if not tgt or head == tgt:
            return False                   # already current
        return git("checkout", "--quiet", "--detach", ref, t=15).returncode == 0
    except Exception:
        return False


# ── the sessions adapter (drill your own agent history) — managed defaults, zero config ──────────────
SESSIONS_HOME = os.environ.get("DRILLABLE_HOME") or os.path.expanduser("~/.drillable")
SESSIONS_CFG = os.path.join(SESSIONS_HOME, "sessions.json")
SESSIONS_FACTS = os.path.join(SESSIONS_HOME, "sessions")
SESSIONS_SOURCE = os.environ.get("DRILLABLE_SESSIONS_SOURCE") or os.path.expanduser("~/.claude/projects")


def _import_sessions():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters"))
    import sessions
    return sessions


def _auto_convert(cfg):
    """For an `adapter: sessions` corpus, keep the facts_dir current by converting NEW transcripts from the
    source (~/.claude/projects) — the 'updated user path', zero-command. Incremental + throttled (like the
    auto-fetch), so the query hot path only ever touches genuinely-new sessions. Returns True iff it wrote
    any fresh `.md` (→ the caller reseeds). Safe/best-effort: any error → False, the last good index stands."""
    if cfg.get("adapter") != "sessions":
        return False
    marker = cfg["_db"] + ".autoconv"
    try:
        if os.path.exists(marker) and (time.time() - os.path.getmtime(marker)) < AUTO_FETCH_THROTTLE:
            return False
        open(marker, "a").close()
        os.utime(marker, None)
    except Exception:
        pass
    try:
        r = _import_sessions().convert(cfg["facts_dir"], cfg.get("source") or SESSIONS_SOURCE)
        return bool(r.get("fresh"))
    except Exception:
        return False


def con(cfg):
    db = cfg["_db"]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import seed
    # (Re)seed when: an opt-in auto_refresh just advanced a clean worktree to its pinned ref, the DB is
    # absent, a fact changed since it was built (freshness), OR the DB's stamped schema version != the
    # code's — so a plugin update that changes SCHEMA self-heals on the next query instead of erroring
    # against a stale-shape DB. seed.main() rebuilds in place (.md stay the truth).
    needs = (_auto_convert(cfg)                    # sessions adapter: pull in new transcripts (throttled)
             or _auto_advance(cfg)
             or not os.path.exists(db)
             or facts_mtime(cfg) > os.path.getmtime(db)
             or _db_schema_version(db) != seed.SCHEMA_VERSION)
    if needs:
        with contextlib.redirect_stdout(sys.stderr):  # seed reads the SAME sys.argv the server got
            try:
                seed.main()
            except SystemExit:
                pass
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def toks(s):
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in STOP and len(t) > 1]


EMBED_FLOOR = float(os.environ.get("DRILLABLE_EMBED_FLOOR", "0.30"))  # cosine below ≈ off-topic → abstain (tunable)
EMBED_BAND = float(os.environ.get("DRILLABLE_EMBED_BAND", "0.10"))    # also drop hits this far below the top —
#                                                                       trims weak padding so a near-miss returns few, not a full page
KEYWORD_FLOOR = 2   # keyword: pass on a title hit OR ≥ this body/overlap weight; a lone common-word body match (=1) abstains
MAX_HITS = 8
# Query-conditional rerank (opt-in): when the top embedding cosine is BELOW this, the query is
# low-confidence (typically a plain-worded/vocab-foreign question — measured ~0.51 vs ~0.68 for
# term-matching queries), so an LLM reorders the top-K candidates. Fires on ~the third of queries that
# need it (lifts everyday-question recall@5 ~78→93% on the benchmark) and skips the rest, so the
# serve-time LLM call is paid only where retrieval is unsure.
RERANK_FLOOR = float(os.environ.get("DRILLABLE_RERANK_FLOOR", "0.60"))
RERANK_K = int(os.environ.get("DRILLABLE_RERANK_K", "20"))


def _best_by_slug(scored):
    """scored: iterable of (score, chunk_row) → the top-scoring SECTION per fact, ranked desc.

    Retrieval ranks sections; results are still one-per-fact (the section is what gets shown +
    drilled, but `get` returns the whole fact). So a multi-section doc can't crowd the page with
    its own sections, and the snippet is the matching SECTION, not the file's opening line."""
    best = {}
    for s, r in scored:
        if r["slug"] not in best or s > best[r["slug"]][0]:
            best[r["slug"]] = (s, r)
    return sorted(best.values(), key=lambda x: -x[0])


def _rerank(query, cands):
    """An LLM reorders the top-K candidate sections by relevance to the query (cross-encoder style).
    cands: list of (cosine, row) → same items, reordered. Degrades to the input order on any error or
    missing key, so it is never worse than no rerank. Caller gates this on a low-confidence query."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key or not cands:
        return cands
    listing = "\n".join(
        f"{i+1}. {r['title']}" + (f" § {r['heading']}" if r["heading"] else "")
        + " — " + re.sub(r"\s+", " ", r["text"])[:240]
        for i, (_s, r) in enumerate(cands))
    body = json.dumps({"model": os.environ.get("DRILLABLE_RERANK_MODEL", "gpt-4o-mini"), "temperature": 0,
        "messages": [
            {"role": "system", "content": "Rank the numbered documents by how well each answers the "
             "question, best first. Reply with ONLY the numbers, comma-separated, best first."},
            {"role": "user", "content": f"Question: {query}\n\nDocuments:\n{listing}"}]}).encode()
    try:
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        out = json.load(urllib.request.urlopen(req, timeout=30))["choices"][0]["message"]["content"]
    except Exception:
        return cands
    seen, order = set(), []
    for tok in re.findall(r"\d+", out):
        i = int(tok) - 1
        if 0 <= i < len(cands) and i not in seen:
            seen.add(i); order.append(cands[i])
    for i, c in enumerate(cands):                    # append any the model dropped, in cosine order
        if i not in seen:
            order.append(c)
    return order


def _retriever(cfg):
    """(mode, label) — the retriever ACTUALLY live, mirroring v_search's decision (a vector for every
    chunk AND a key available). Lets stats/search state the truth instead of silently running keyword
    when the user asked for semantic."""
    c = con(cfg)
    total = c.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
    vecs = c.execute("SELECT COUNT(*) FROM chunk WHERE vector IS NOT NULL").fetchone()[0]
    if total and vecs == total and embed.available():
        return "semantic", f"semantic ({embed.MODEL})"
    if cfg.get("embed"):                            # asked for semantic, but it isn't on — say why
        why = ("no OPENAI_API_KEY in this server's environment" if not embed.available()
               else "facts aren't embedded yet — reseed")
        return "keyword", f"keyword — semantic requested but INACTIVE ({why}); ~94% vs ~67% recall@3 once on"
    return "keyword", 'keyword (set "embed": true + OPENAI_API_KEY for semantic — ~94% vs ~67% recall@3)'


def _current_project():
    """The cwd-derived project name — the default retrieval scope for a session corpus (the settled
    'current-project by default'). Best-effort; the plugin passes project= explicitly when it knows better."""
    try:
        return os.path.basename(os.getcwd()) or None
    except Exception:
        return None


def v_search(cfg, query="", project=None):
    if not toks(query):
        return "empty query."
    rows = con(cfg).execute(
        "SELECT c.slug AS slug, c.heading AS heading, c.text AS text, c.vector AS vector, "
        "m.title AS title, m.grounding AS grounding, m.stale AS stale, m.project AS project "
        "FROM chunk c JOIN memory m ON m.slug = c.slug WHERE m.serving='queryable'").fetchall()
    # PROJECT SCOPE (session corpora): filter to one project's facts so a user-level install never
    # contaminates one project's drills with another's. Explicit `project=` wins; else the config's
    # default_project; else the cwd-derived project. `project='all'` (or a corpus with no projects) spans
    # everything. An IMPLICIT default that matches nothing falls back to all (never a silent total-miss); an
    # EXPLICIT project that matches nothing abstains honestly. Every hit is project-labelled below.
    scope_note = ""
    if any(r["project"] for r in rows):
        explicit = project is not None
        scope = project if explicit else (cfg.get("default_project") or _current_project())
        if scope and str(scope).lower() != "all":
            scoped = [r for r in rows if r["project"] == scope]
            if scoped:
                rows, scope_note = scoped, f'\n— scoped to project "{scope}" (project="all" spans every project)'
            elif explicit:
                return f'no record — no facts in project "{scope}" (an honest abstention, not a guess).'
            # implicit default matched nothing → fall through to all (no silent blank)
        if not scope_note:
            projs = sorted({r["project"] for r in rows if r["project"]})
            if len(projs) > 1:
                scope_note = f'\n— spanning {len(projs)} projects (pass project="<name>" to scope)'
    qvec = None
    if rows and all(r["vector"] for r in rows) and embed.available():
        ev = embed.embed([query])
        qvec = ev[0] if ev else None
    reranked = False
    if qvec is not None:                       # section-level embedding retrieval + cosine floor + top-band
        ranked = _best_by_slug((embed.cosine(qvec, json.loads(r["vector"])), r) for r in rows)
        ranked = [(s, r) for s, r in ranked if s >= EMBED_FLOOR]
        if cfg.get("rerank") and ranked and ranked[0][0] < RERANK_FLOOR and embed.available():
            scored = _rerank(query, ranked[:RERANK_K])[:MAX_HITS]   # low-confidence query → LLM reorder
            reranked = True
        else:                                  # confident query → cosine top-band (no LLM call)
            if ranked:
                top = ranked[0][0]
                ranked = [(s, r) for s, r in ranked if s >= top - EMBED_BAND]
            scored = ranked[:MAX_HITS]
    else:                                      # keyword fallback — abstains on no overlap AND on a lone weak match
        q = set(toks(query))
        kw = []
        for r in rows:
            tt, bt = set(toks(r["title"])), toks(r["text"])
            title_hits = sum(t in q for t in tt)
            sc = 3 * title_hits + sum(min(bt.count(t), 3) for t in q)
            if title_hits or sc >= KEYWORD_FLOOR:   # floor: a single common-word body overlap (sc=1) abstains
                kw.append((sc, r))
        scored = _best_by_slug(kw)[:MAX_HITS]
    if not scored:
        return f'no record — "{query}" misses (an honest abstention, not a guess).'
    out = [f'search "{query}" → top {len(scored)}:']
    for sc, r in scored:
        sec = f" § {r['heading']}" if r["heading"] else ""
        proj = f" · {r['project']}" if r["project"] else ""   # cross-project hits are never silent (project-labelled)
        warn = " ⚠ may be stale" if (r["stale"] and r["stale"] != "[]") else ""
        snip = re.sub(r"\s+", " ", r["text"])[:140]
        out.append(f"\n({sc:.2f}) {r['slug']}{proj}{sec}  [{MARK[r['grounding']]}]{warn}\n  {snip}…")
    if scope_note:
        out.append(scope_note)
    if qvec is None and cfg.get("embed"):          # they asked for semantic but got keyword — tell them once
        out.append(f"\n— retriever: {_retriever(cfg)[1]}")
    elif reranked:                                 # surface that a low-confidence query was LLM-reranked
        out.append("\n— reranked (low-confidence query); order is by relevance, not cosine")
    return "\n".join(out)


def v_get(cfg, slug=""):
    r = con(cfg).execute("SELECT * FROM memory WHERE slug=?", (slug,)).fetchone()
    if not r:
        return f'no record for "{slug}" — abstaining.'
    head = [f"# {r['slug']}   [{r['type'] or '—'} · {r['serving']} · {r['grounding']}]",
            f"source of truth: {r['source_file']}"]
    anchors = json.loads(r["anchors"])
    if anchors:
        head.append(f"anchors: {anchors}  ({r['anchors_ok']} re-resolve)")
    if r["origin_session"]:
        head.append(f"origin: {r['origin_session']}  (provenance — log likely pruned)")
    asof = r["asof"] if "asof" in r.keys() else None
    if asof:
        cr = r["change_rate"] if "change_rate" in r.keys() else None
        head.append(f"as-of: {asof[:10]}" + (f"  (change-rate: {cr})" if cr else ""))
    stale = json.loads(r["stale"]) if ("stale" in r.keys() and r["stale"]) else []
    if stale:
        head.append(f"⚠ may be stale — cited source changed after this was written, re-verify: {stale}")
    return "\n".join(head) + "\n\n" + r["body"].strip()


def v_standing(cfg):
    rows = con(cfg).execute("SELECT title,type FROM memory WHERE serving='standing' ORDER BY type").fetchall()
    return f"STANDING — {len(rows)} always-loaded:\n" + "\n".join(f"  [{r['type'] or '—'}] {r['title']}" for r in rows)


def _facts_behind(facts_dir):
    """(n_behind, upstream) if the facts checkout is behind its tracked remote, else None.

    The per-CORPUS staleness signal (the per-fact as-of can't see it): if the indexing checkout is behind
    its remote, the .md are old *content* the corpus would serve as current. Best-effort + offline-safe —
    compares HEAD to the LOCAL remote-tracking ref (no fetch); skips silently when facts_dir isn't a git
    repo or has no upstream, exactly like embeddings degrade. Surfaces only; never pulls (a pull can
    clobber uncommitted work)."""
    try:
        up = subprocess.run(["git", "-C", facts_dir, "rev-parse", "--abbrev-ref",
                             "--symbolic-full-name", "@{upstream}"],
                            capture_output=True, text=True, timeout=5)
        if up.returncode != 0 or not up.stdout.strip():
            return None
        upstream = up.stdout.strip()
        n = subprocess.run(["git", "-C", facts_dir, "rev-list", "--count", f"HEAD..{upstream}"],
                           capture_output=True, text=True, timeout=5)
        if n.returncode != 0:
            return None
        cnt = int(n.stdout.strip() or "0")
        return (cnt, upstream) if cnt > 0 else None
    except Exception:
        return None


def v_stats(cfg):
    c = con(cfg)
    tot = c.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    sv = ", ".join(f"{r[0]} {r[1]}" for r in c.execute("SELECT serving,COUNT(*) FROM memory GROUP BY serving"))
    gr = ", ".join(f"{r[0]} {r[1]}" for r in c.execute("SELECT grounding,COUNT(*) FROM memory GROUP BY grounding"))
    out = f"{cfg['name']} — {tot} facts\n  serving: {sv}\n  grounding: {gr}\n  retriever: {_retriever(cfg)[1]}"
    if cfg.get("auto_refresh"):
        out += f"\n  auto-refresh: {cfg['auto_refresh']} (advances a clean worktree on query; throttled)"
    behind = _facts_behind(cfg["facts_dir"])
    if behind:
        n, up = behind
        out += (f"\n  ⚠ facts checkout is {n} commit(s) behind {up} — may be serving stale facts; "
                f"pull to refresh (not done automatically — a pull can clobber local work)")
    return out


ENUM_AXES = ("collection", "grounding", "type", "serving")   # honest facets to slice the corpus by
ENUM_CAP = 60   # members rendered when listing one category; the COUNT stays exact (the completeness bit)


def _collection(source_file):
    """Top path segment of a fact's source (findings / decisions / …) — the natural 'what kind' axis;
    '(root)' for a top-level file."""
    p = (source_file or "").replace("\\", "/").lstrip("./")
    return p.split("/", 1)[0] if "/" in p else "(root)"


def v_enumerate(cfg, by="collection", kind=""):
    """The enumerate SHAPE — the complete set sliced by a facet, with an explicit completeness bit.
    No `kind`: the directory (each facet value + its exact count — the 'what does this corpus hold' view
    search can't give). With `kind`: that category's members (slug — title, capped; the count stays
    exact). Retrieval-grade — it lists and cites; it never re-derives an answer (no oracle behind a doc)."""
    by = (by or "collection").strip().lower()
    if by not in ENUM_AXES:
        return f'enumerate: unknown axis "{by}" — use one of: {", ".join(ENUM_AXES)}.'
    rows = con(cfg).execute(
        "SELECT slug, title, type, grounding, serving, source_file FROM memory ORDER BY slug").fetchall()
    if not rows:
        return f"{cfg['name']} — 0 facts."
    val = (lambda r: _collection(r["source_file"])) if by == "collection" else (lambda r: r[by] or "—")
    total = len(rows)
    counts = {}
    for r in rows:
        k = val(r)
        counts[k] = counts.get(k, 0) + 1
    order = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if not kind:                                     # the directory: exact counts over the WHOLE set → complete
        lines = [f"{cfg['name']} — {total} facts, by {by}  [complete set]:"]
        lines += [f"  {k} · {n}" for k, n in order]
        lines.append(f'\ndrill a category: enumerate(by="{by}", kind="{order[0][0]}")')
        return "\n".join(lines)
    members = [r for r in rows if val(r) == kind]    # one category → its members (exact count, capped render)
    n = len(members)
    if not n:
        return (f'{cfg["name"]} — no facts with {by}="{kind}". '
                f'available: {", ".join(k for k, _ in order)}.')
    shown = members[:ENUM_CAP]
    tag = "[complete]" if len(shown) == n else f"[showing {len(shown)} of {n} — capped, not complete]"
    lines = [f'{cfg["name"]} — {by}="{kind}": {n} facts  {tag}:']
    for r in shown:
        t = re.sub(r"\s+", " ", (r["title"] or "")).strip()
        lines.append(f'  {r["slug"]} — {t[:80]}  [{MARK[r["grounding"]]}]')
    return "\n".join(lines)


def build_tools(cfg):
    n = cfg["name"]
    spec = [("search", "Search the fetch-on-demand facts (decisions, conventions, gotchas). Abstains on a miss. "
             "For a session corpus, scope defaults to the current project; pass project=\"<name>\" to target "
             "one, or project=\"all\" to span every project.",
             {"query": {"type": "string"},
              "project": {"type": "string", "description": "optional — scope to one project (session corpora); "
                          "'all' spans every project. Defaults to the current project."}},
             ["query"], lambda a: v_search(cfg, **a)),
            ("enumerate",
             "The complete set by category (collection / grounding / type / serving) + a completeness bit — "
             "the broad 'what does this corpus hold' view search can't give. Pass kind= to list one "
             "category's members. Retrieval-grade: it lists and cites, never computes an answer.",
             {"by": {"type": "string", "description": "facet: collection | grounding | type | serving (default collection)"},
              "kind": {"type": "string", "description": "optional — list this category's members instead of the directory"}},
             [], lambda a: v_enumerate(cfg, **a)),
            ("get", "One fact by slug — full body + grounding verdict (cited/provenance/judgment) + anchors.",
             {"slug": {"type": "string"}}, ["slug"], lambda a: v_get(cfg, **a)),
            ("standing", "The always-loaded standing instructions (preferences / who you are).",
             {}, [], lambda a: v_standing(cfg)),
            ("stats", "The split (standing vs queryable) + grounding breakdown.",
             {}, [], lambda a: v_stats(cfg))]
    tools = {}
    for verb, desc, props, req, fn in spec:
        tools[f"{n}_{verb}"] = {
            "name": f"{n}_{verb}", "description": desc,
            "inputSchema": {"type": "object", "properties": props, **({"required": req} if req else {})},
            "fn": fn}
    return tools


def handle(req, cfg, tools):
    m, rid = req.get("method"), req.get("id")
    if m == "initialize":
        # negotiate: echo the client's protocolVersion (founding `tools` surface, valid 2024-11-05 →
        # current 2025-11-25; the 2026-07-28 RC removes this handshake — known future migration).
        ver = (req.get("params") or {}).get("protocolVersion") or "2024-11-05"
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": ver, "capabilities": {"tools": {}},
            "serverInfo": {"name": f"{cfg['name']}-context", "version": VERSION}}}
    if m == "notifications/initialized":
        return None
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {k: t[k] for k in ("name", "description", "inputSchema")} for t in tools.values()]}}
    if m == "tools/call":
        p = req.get("params", {})
        t = tools.get(p.get("name"))
        if not t:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown tool"}}
        try:
            text = t["fn"](p.get("arguments") or {})
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {m}"}}
    return None


def setup_sessions(argv):
    """The FRESH user path — one command, zero config: `drillable-context sessions`. Writes a managed
    config with good defaults, converts ~/.claude/projects → ~/.drillable/sessions, seeds, and prints the
    one line that wires the MCP. Thereafter the server keeps it current on its own (see _auto_convert)."""
    import argparse
    ap = argparse.ArgumentParser(prog="drillable-context sessions",
                                 description="index your Claude Code session history for grounded drilling")
    ap.add_argument("--projects-dir", default=SESSIONS_SOURCE, help="~/.claude/projects (default)")
    ap.add_argument("--rebuild", action="store_true", help="re-convert every session (ignore the incremental skip)")
    a = ap.parse_args(argv)

    os.makedirs(SESSIONS_FACTS, exist_ok=True)
    cfg = {"name": "sessions", "adapter": "sessions", "facts_dir": SESSIONS_FACTS,
           "source": os.path.expanduser(a.projects_dir), "oracle_repo": None, "standing_types": [],
           "type_field": "type", "recursive": True, "embed": True, "doc2query": False, "rerank": False}
    with open(SESSIONS_CFG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)

    print(f"drillable-context sessions — indexing {a.projects_dir}", file=sys.stderr)
    r = _import_sessions().convert(SESSIONS_FACTS, a.projects_dir, rebuild=a.rebuild)
    if r.get("error"):
        sys.exit(r["error"])
    print(f"  {r['fresh']} new/updated · {r['written']} sessions total", file=sys.stderr)

    import seed
    old = sys.argv
    sys.argv = ["seed", "--config", SESSIONS_CFG]
    try:
        with contextlib.redirect_stdout(sys.stderr):
            seed.main()
    except SystemExit:
        pass
    finally:
        sys.argv = old

    key = "semantic (OPENAI_API_KEY found)" if os.environ.get("OPENAI_API_KEY") else \
        "keyword only — set OPENAI_API_KEY for semantic search"
    print(f"\n✓ session history indexed · {key}", file=sys.stderr)
    print("  wire it as an MCP (once):", file=sys.stderr)
    print(f"    claude mcp add drillable-sessions -- npx drillable-context --config {SESSIONS_CFG}", file=sys.stderr)
    print("  then ask:  drillable-sessions_search \"what did I do about <x>\"  (scoped to the current project;"
          " project=\"all\" spans every repo)", file=sys.stderr)
    print("  new sessions are picked up automatically — re-run this only to force a rebuild.", file=sys.stderr)


def main():
    if sys.argv[1:2] == ["sessions"]:      # the fresh-user setup path (not the MCP stdio server)
        return setup_sessions(sys.argv[2:])
    cfg = load_cfg(sys.argv[1:])
    tools = build_tools(cfg)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req, cfg, tools)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
