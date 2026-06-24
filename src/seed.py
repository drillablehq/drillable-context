#!/usr/bin/env python3
"""seed.py — build a private-facts corpus DB from a directory of markdown facts.

Config-driven: point it at
ANY folder of `*.md` — your CLAUDE.md, your docs/, your decisions/notes — and it derives a queryable,
citation-grounded store. The `.md` files stay the SOURCE OF TRUTH; the DB is a rebuilt build artifact.

  python3 src/seed.py --config configs/<name>.json

config.json:
  {
    "name":           "context",                # corpus name → <name>.db, the MCP server name
    "facts_dir":      "/abs/path/to/markdown",  # where the facts live (recursive *.md)
    "oracle_repo":    "/abs/path/to/repo",      # OPTIONAL: re-resolve file/PR anchors here (the
                                                #   stronger oracle); null → ground against the .md itself
    "standing_types": ["preference"],           # frontmatter `type` values that stay ALWAYS-LOADED
    "type_field":     "type",                   # frontmatter field carrying the type (default "type")
    "index_file":     "INDEX.md",               # OPTIONAL: "- [Title](slug.md) — hook" lines for titles
    "track_drift":    false,                    # OPTIONAL: flag a cited fact whose source changed after
                                                #   it (re-verify). Off by default — noisy in a co-evolving
                                                #   monorepo; signal when oracle_repo is a SEPARATE code repo
    "doc2query":      <follows embed>          # index-side expansion (docTTTTTquery): append LLM-predicted
                                                #   lay-questions to each chunk's EMBEDDING text only (never
                                                #   the served text) — lifts recall on a lay-worded query the
                                                #   chunk's own vocabulary doesn't match. BUNDLES WITH EMBED:
                                                #   on whenever "embed" is, since it's the same data to the
                                                #   same vendor for ~pennies. Set false here (or --no-doc2query
                                                #   / DRILLABLE_DOC2QUERY=false) to opt out. Cached by content
                                                #   hash; query path unchanged.
  }

THE SPLIT: standing (must fire every turn — preferences/standing instructions) vs queryable (the
fetch-on-demand tail). THE ORACLE (honest, the weakest useful kind): CITED (an anchor re-resolves) ⟂
PROVENANCE (a dated origin, log likely gone) ⟂ JUDGMENT (a preference, no external source — never
graded 'verified'). It stops your agent bluffing your facts and abstains when it can't ground them;
it is NOT a truth oracle.
"""
import argparse
import concurrent.futures
import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import embed  # noqa: E402 — local; optional embedding retriever with graceful no-key fallback
import config  # noqa: E402 — local; resolves a corpus config from a file or params/env

PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|ts|tsx|js|sql|json|sh|ya?ml|html|toml|md|go|rs|java|rb|c|cpp|h)\b")
PR_RE = re.compile(r"#(\d{2,5})\b")
LINK_RE = re.compile(r"\[\[([a-z0-9-]+)\]\]")
HEAD_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
SECTION_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$", re.M)


def split_sections(body):
    """[(heading|None, text)] — the intro (before the first ## ) then each ##-section, in order.

    The retrieval UNIT. A fact with no sub-headings → one chunk = its whole body, so the
    one-fact-per-file case is byte-unchanged; a big multi-section doc → one chunk per section, so
    search can point at the SECTION, not the whole file."""
    marks = list(SECTION_RE.finditer(body))
    if not marks:
        b = body.strip()
        return [(None, b)] if b else []
    out = []
    intro = body[:marks[0].start()].strip()
    if intro:
        out.append((None, intro))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append((m.group(2).strip(), body[m.start():end].strip()))
    return out

def doc2query_aug(etexts, cache_path, model="gpt-4o-mini", workers=12):
    """doc2query / docTTTTTquery (Nogueira & Lin 2019): for each chunk, predict the lay QUESTIONS it
    answers and append them to the EMBEDDING text only — never to the served text. They are a retrieval
    KEY, not content (so honesty is preserved: nothing synthetic is ever shown or cited). This lifts
    vocab-foreign recall — a query phrased in everyday words whose terms the chunk itself never uses
    (measured: foreign recall@1 25→67%, @3 58→83%, with no precision cost; see the hub decision
    `context-search-vocab-foreign-fix-stays-per-finding-alias`). Cached by content hash, so a re-seed
    regenerates ONLY changed chunks. Offline/one-time; the query path is unchanged.

    Generation is PARALLEL (the calls are independent I/O), so a first seed stays seconds, not minutes.
    A per-chunk failure degrades to no augmentation for that chunk (left uncached → retried next seed),
    so a transient error never tanks the whole seed. Returns (augmented_texts, n_generated)."""
    key = os.environ.get("OPENAI_API_KEY")
    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path))
        except Exception:
            cache = {}
    SYS = ("You are given a section of a software project's notes. Write 3 short, DISTINCT questions a "
           "user might ask that this section answers — phrased in plain everyday vocabulary, NOT the "
           "section's own jargon (use lay synonyms for technical terms). One per line, no numbering.")

    def _gen(et):
        data = json.dumps({"model": model, "temperature": 0.4, "messages": [
            {"role": "system", "content": SYS}, {"role": "user", "content": et[:4000]}]}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=data,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"].strip()

    # unique uncached chunks only (identical sections share a hash → generate once), run concurrently
    todo = {}
    for et in etexts:
        h = hashlib.sha256(et.encode()).hexdigest()
        if h not in cache:
            todo.setdefault(h, et)
    made = 0
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_gen, et): h for h, et in todo.items()}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    cache[futs[fut]] = fut.result()
                    made += 1
                except Exception:
                    pass   # leave uncached → retried next seed; this chunk just isn't augmented this run
        json.dump(cache, open(cache_path, "w"))
    out = []
    for et in etexts:
        h = hashlib.sha256(et.encode()).hexdigest()
        out.append(et + ("\n\n" + cache[h] if h in cache else ""))
    return out, made


# Bump on EVERY change to SCHEMA below. Stamped into the DB (PRAGMA user_version) at seed; the server
# reseeds when a DB's stamp != this, so a schema-changing plugin update self-heals instead of erroring
# against a stale-shape DB. (v1 = chunk table + as-of columns; pre-stamp DBs read as 0 → mismatch → reseed.)
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE memory (
  slug TEXT PRIMARY KEY, type TEXT NOT NULL, serving TEXT NOT NULL, title TEXT NOT NULL,
  body TEXT NOT NULL, source_file TEXT NOT NULL, grounding TEXT NOT NULL,
  anchors TEXT NOT NULL DEFAULT '[]', anchors_ok INTEGER NOT NULL DEFAULT 0,
  origin_session TEXT, links TEXT NOT NULL DEFAULT '[]',
  asof TEXT, stale TEXT NOT NULL DEFAULT '[]', change_rate TEXT
);
CREATE INDEX idx_serving ON memory(serving);
CREATE INDEX idx_grounding ON memory(grounding);
CREATE TABLE chunk (
  slug TEXT NOT NULL, ord INTEGER NOT NULL, heading TEXT, text TEXT NOT NULL, vector TEXT
);
CREATE INDEX idx_chunk_slug ON chunk(slug);
"""


def fm_field(key, text):
    m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text, re.M)
    return m.group(1).strip().strip('"') if m else None


def split_frontmatter(raw):
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            return raw[3:end], raw[end + 4:].lstrip("\n")
    return "", raw


def repo_index(repo):
    """(full-relpaths, basenames) of tracked repo files — resolves bare-filename anchors too."""
    if not repo or not os.path.isdir(repo):
        return set(), set()
    files = []
    try:
        out = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True, timeout=20)
        files = [f for f in out.stdout.split("\n") if f] if out.returncode == 0 else []
    except Exception:
        files = []
    if not files:
        for root, _, fs in os.walk(repo):
            if os.sep + ".git" in root:
                continue
            files += [os.path.relpath(os.path.join(root, fn), repo) for fn in fs]
    return set(files), {os.path.basename(f) for f in files}


ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT")


def last_commit_map(repo):
    """{relpath: ISO datetime of the most recent commit that touched it} — one git pass, newest-first.

    The 'as-of' signal: when each file last changed in git. Empty when the repo isn't a git tree, so
    the engine degrades to file mtime (below) and drift detection just stays off."""
    if not repo or not os.path.isdir(repo):
        return {}
    try:
        out = subprocess.run(["git", "-C", repo, "log", "--format=%cI", "--name-only", "--no-renames"],
                             capture_output=True, text=True, timeout=60)
    except Exception:
        return {}
    if out.returncode != 0:
        return {}
    m, cur = {}, None
    for line in out.stdout.split("\n"):
        if not line:
            continue
        if ISO_RE.match(line):
            cur = line.strip()
        elif cur:
            m.setdefault(line.strip(), cur)   # newest-first stream → first occurrence is the last commit
    return m


def _ts(iso):
    """ISO datetime → POSIX seconds (tz-aware compare; bad/empty → 0). Lexical ISO compare is unsafe
    across differing tz offsets, so we parse."""
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def _mtime_iso(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat(timespec="seconds")
    except Exception:
        return None


def parse_index_titles(facts_dir, index_file):
    titles = {}
    if not index_file:
        return titles
    path = os.path.join(facts_dir, index_file)
    if not os.path.exists(path):
        return titles
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^- \[(.+?)\]\(([a-z0-9-]+)\.md\)\s*—\s*(.+)$", line.strip())
            if m:
                title, slug, hook = m.groups()
                titles[slug] = f"{title} — {hook}"
    return titles


def main():
    cfg = config.resolve(sys.argv[1:])
    name = cfg["name"]
    facts_dir = cfg["facts_dir"]
    oracle_repo = cfg.get("oracle_repo")
    standing_types = set(cfg.get("standing_types", []))
    type_field = cfg.get("type_field", "type")
    index_file = cfg.get("index_file")
    track_drift = cfg.get("track_drift", False)   # opt-in (see drift note below) — off by default
    db = cfg["_db"]

    if not os.path.isdir(facts_dir):
        sys.exit(f"facts_dir not found: {facts_dir}")

    if os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")   # stamp the schema version for self-heal

    repo_full, repo_base = repo_index(oracle_repo)
    # as-of / drift: when each file last changed in git. facts_commits dates the FACT; oracle_commits
    # dates the cited SOURCE (same repo in the common case). oracle_base resolves bare-filename anchors.
    facts_commits = last_commit_map(facts_dir)
    oracle_commits = facts_commits if (oracle_repo and os.path.abspath(oracle_repo) == os.path.abspath(facts_dir)) \
        else last_commit_map(oracle_repo)
    oracle_base = {}
    for _k, _v in oracle_commits.items():
        _b = os.path.basename(_k)
        if _b not in oracle_base or _v > oracle_base[_b]:
            oracle_base[_b] = _v
    titles = parse_index_titles(facts_dir, index_file)
    recursive = cfg.get("recursive", True)
    exclude = cfg.get("exclude", [])
    respect_gitignore = cfg.get("respect_gitignore", True)

    # Prefer git: list .md that git does NOT ignore — skips node_modules, build output, AND secrets
    # (.env / keys are almost always gitignored, so they're never indexed or embedded). Falls back to
    # a plain glob when facts_dir isn't inside a git repo.
    candidates = None
    if respect_gitignore:
        try:
            r = subprocess.run(["git", "-C", facts_dir, "ls-files", "--cached", "--others",
                                "--exclude-standard", "-z"], capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                candidates = [os.path.join(facts_dir, p) for p in r.stdout.split("\0") if p.endswith(".md")]
        except Exception:
            candidates = None
    if candidates is None:
        pattern = os.path.join(facts_dir, "**", "*.md") if recursive else os.path.join(facts_dir, "*.md")
        candidates = glob.glob(pattern, recursive=recursive)

    def _keep(p):
        rel = os.path.relpath(p, facts_dir)
        if not recursive and os.sep in rel:                      # top-level only
            return False
        if os.sep + "." in os.sep + rel:                         # skip dot-dirs/files
            return False
        if os.path.basename(p) == (index_file or ""):            # the index isn't a fact
            return False
        return not any(x in rel for x in exclude)                # extra config excludes (e.g. tracked dirs)

    files = sorted(p for p in candidates if _keep(p))
    counts = {"standing": 0, "queryable": 0, "cited": 0, "provenance": 0, "judgment": 0, "ok": 0, "stale": 0}
    chunks = []   # (slug, ord, heading, text, title) — the retrieval units (one per ## section)
    seen_slugs = set()

    for path in files:
        slug = os.path.splitext(os.path.basename(path))[0]
        if slug in seen_slugs:                       # two files share a basename (e.g. cli/README + README)
            parent = os.path.basename(os.path.dirname(path)) or "root"
            base, slug, k = slug, f"{parent}-{slug}", 2
            while slug in seen_slugs:
                slug, k = f"{parent}-{base}-{k}", k + 1
        seen_slugs.add(slug)
        raw = open(path, encoding="utf-8").read()
        fm, body = split_frontmatter(raw)
        mtype = (fm_field(type_field, fm) or "").strip() or None
        origin = fm_field("originSessionId", fm)
        heading = (HEAD_RE.search(body) or [None, None])[1] if HEAD_RE.search(body) else None
        title = titles.get(slug) or heading or fm_field("description", fm) or slug

        serving = "standing" if mtype in standing_types else "queryable"

        scan = fm + "\n" + body
        paths = sorted({m.group(0) for m in PATH_RE.finditer(scan)})
        anchors = paths + sorted({f"#{n}" for n in PR_RE.findall(scan)})
        anchors_ok = sum(1 for p in paths if p in repo_full or os.path.basename(p) in repo_base)

        if mtype in standing_types:
            grounding = "judgment"          # a standing instruction = a preference, no external oracle
        elif anchors:
            grounding = "cited"
        elif origin:
            grounding = "provenance"
        else:
            grounding = "judgment"

        rel = os.path.relpath(path, facts_dir)
        asof = facts_commits.get(rel) or _mtime_iso(path)
        change_rate = fm_field("change_rate", fm) or fm_field("changeRate", fm)
        # Drift: a CITED fact whose anchored source file changed AFTER the fact itself was last touched —
        # a "re-verify" prompt, NOT a claim the fact is wrong (surface ⊥ adjudicate). OPT-IN (`track_drift`):
        # it's signal when facts are authored against a SEPARATE, slower code repo, but in a co-evolving
        # monorepo (facts == the repo's own docs, co-committed with code) it cries wolf — measured 82% on
        # the hub corpus. Off by default; the as-of stamp above is always on and noise-free.
        stale = []
        if track_drift and asof:
            for p in paths:
                d = oracle_commits.get(p) or oracle_base.get(os.path.basename(p))
                if d and _ts(d) > _ts(asof):
                    stale.append(p)
        stale = sorted(set(stale))

        for ordn, (sec_head, sec_text) in enumerate(split_sections(body)):
            chunks.append((slug, ordn, sec_head, sec_text, title))
        counts[serving] += 1
        counts[grounding] += 1
        counts["ok"] += anchors_ok
        counts["stale"] += 1 if stale else 0
        con.execute("INSERT INTO memory(slug,type,serving,title,body,source_file,grounding,anchors,"
                    "anchors_ok,origin_session,links,asof,stale,change_rate) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (slug, mtype or "", serving, title, body, rel,
                     grounding, json.dumps(anchors), anchors_ok, origin,
                     json.dumps(sorted(set(LINK_RE.findall(body)))), asof, json.dumps(stale), change_rate))
    con.commit()
    # Embed at the SECTION level — the title rides in each chunk's text so the chunk is self-locating.
    def _etext(head_, text_, title_):
        return f"{title_}\n{head_}\n{text_}" if head_ else f"{title_}\n{text_}"
    embedded = 0
    do_embed = bool(cfg.get("embed") and embed.available())
    etexts = [_etext(h, t, ti) for _, _, h, t, ti in chunks]
    d2q_made = 0
    if do_embed and cfg.get("doc2query"):
        # index-side expansion: augment the EMBEDDING text only (served text untouched). Cache beside the DB.
        d2q_cache = os.path.join(os.path.dirname(os.path.abspath(db)) or ".", f".{name}.d2q.json")
        etexts, d2q_made = doc2query_aug(etexts, d2q_cache)
    vecs = embed.embed(etexts) if do_embed else None
    for i, (s, ordn, h, t, _ti) in enumerate(chunks):
        vec = json.dumps(vecs[i]) if vecs else None
        con.execute("INSERT INTO chunk(slug,ord,heading,text,vector) VALUES(?,?,?,?,?)",
                    (s, ordn, h, t, vec))
        if vec:
            embedded += 1
    con.commit()
    con.close()
    print(f"seeded {db}  ·  {len(files)} facts · {len(chunks)} chunks from {facts_dir}")
    if embedded:
        print(f"  retriever: EMBEDDING — {embedded} chunk vectors (text-embedding-3-small)"
              + (f" · doc2query ON ({d2q_made} chunks expanded this run, rest cached)" if cfg.get("doc2query") else ""))
    elif cfg.get("embed"):
        print("  retriever: keyword (embed:true but no OPENAI_API_KEY found — fell back)")
    else:
        print('  retriever: keyword (set "embed": true + OPENAI_API_KEY to enable embeddings)')
    print(f"  split:     {counts['standing']} standing · {counts['queryable']} queryable")
    print(f"  grounding: {counts['cited']} cited · {counts['provenance']} provenance · {counts['judgment']} judgment"
          + (f"  ({counts['ok']} anchors re-resolve in {os.path.basename(oracle_repo)})" if oracle_repo else ""))
    if facts_commits:
        print(f"  as-of:     {len([1 for _ in files])} facts dated from git"
              + (f" · drift ON · {counts['stale']} flagged stale (cited source changed after the fact — re-verify)"
                 if track_drift else " · drift OFF (set \"track_drift\": true for separate-oracle corpora)"))


if __name__ == "__main__":
    main()
