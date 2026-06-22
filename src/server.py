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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import embed  # noqa: E402 — optional embedding retriever; keyword fallback when no key
import config  # noqa: E402 — resolves a corpus config from a file or params/env

STOP = set("the a an of to in is are for and or it its on at as be by we you i with not this that "
           "these those how what why when who do does".split())
MARK = {"cited": "⛓ cited", "provenance": "◷ provenance", "judgment": "· judgment"}


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


def con(cfg):
    db = cfg["_db"]
    # (Re)seed when the DB is absent OR a fact changed since it was built — so the running server reflects
    # edits, not a stale snapshot. seed.main() rebuilds in place (the .md stay the source of truth).
    if not os.path.exists(db) or facts_mtime(cfg) > os.path.getmtime(db):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import seed
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


EMBED_FLOOR = 0.30  # cosine below this ≈ off-topic → abstain (text-embedding-3-small; tunable, cf. gateway nearest.ts)
KEYWORD_FLOOR = 2   # keyword: pass on a title hit OR ≥ this body/overlap weight; a lone common-word body match (=1) abstains


def v_search(cfg, query=""):
    if not toks(query):
        return "empty query."
    rows = con(cfg).execute("SELECT slug,title,body,grounding,vector FROM memory WHERE serving='queryable'").fetchall()
    qvec = None
    if rows and all(r["vector"] for r in rows) and embed.available():
        ev = embed.embed([query])
        qvec = ev[0] if ev else None
    if qvec is not None:                       # embedding retrieval + cosine floor (keeps the honest abstain)
        ranked = sorted(((embed.cosine(qvec, json.loads(r["vector"])), r) for r in rows), key=lambda x: -x[0])
        scored = [(s, r) for s, r in ranked if s >= EMBED_FLOOR][:8]
    else:                                      # keyword fallback — abstains on no overlap AND on a lone weak match
        q = set(toks(query))
        scored = []
        for r in rows:
            tt, bt = set(toks(r["title"])), toks(r["body"])
            title_hits = sum(t in q for t in tt)
            sc = 3 * title_hits + sum(min(bt.count(t), 3) for t in q)
            if title_hits or sc >= KEYWORD_FLOOR:   # floor: a single common-word body overlap (sc=1) abstains
                scored.append((sc, r))
        scored.sort(key=lambda x: -x[0])
        scored = scored[:8]
    if not scored:
        return f'no record — "{query}" misses (an honest abstention, not a guess).'
    out = [f'search "{query}" → top {len(scored)}:']
    for sc, r in scored:
        snip = re.sub(r"\s+", " ", r["body"])[:140]
        out.append(f"\n({sc:.2f}) {r['slug']}  [{MARK[r['grounding']]}]\n  {snip}…")
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
    return "\n".join(head) + "\n\n" + r["body"].strip()


def v_standing(cfg):
    rows = con(cfg).execute("SELECT title,type FROM memory WHERE serving='standing' ORDER BY type").fetchall()
    return f"STANDING — {len(rows)} always-loaded:\n" + "\n".join(f"  [{r['type'] or '—'}] {r['title']}" for r in rows)


def v_stats(cfg):
    c = con(cfg)
    tot = c.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    sv = ", ".join(f"{r[0]} {r[1]}" for r in c.execute("SELECT serving,COUNT(*) FROM memory GROUP BY serving"))
    gr = ", ".join(f"{r[0]} {r[1]}" for r in c.execute("SELECT grounding,COUNT(*) FROM memory GROUP BY grounding"))
    return f"{cfg['name']} — {tot} facts\n  serving: {sv}\n  grounding: {gr}"


def build_tools(cfg):
    n = cfg["name"]
    spec = [("search", "Search the fetch-on-demand facts (decisions, conventions, gotchas). Abstains on a miss.",
             {"query": {"type": "string"}}, ["query"], lambda a: v_search(cfg, **a)),
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
            "serverInfo": {"name": f"{cfg['name']}-context", "version": "0.1.0"}}}
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


def main():
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
