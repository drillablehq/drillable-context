#!/usr/bin/env python3
"""sessions adapter — convert Claude Code session transcripts into the markdown the engine indexes.

The drillable-context engine ingests a folder of `*.md` (seed.py). This is the CLAUDE CODE parser that
produces that folder FROM `~/.claude/projects/**/*.jsonl`: one `<sessionId>.md` per session, chunked by
TURN (one `## section` per user→assistant exchange — the retrieval unit), with frontmatter
`type: session` + `originSessionId:` so the engine grounds each as PROVENANCE (a dated origin — the
existing kind for exactly this; no engine change). Point `configs/sessions.json` at `--out` and seed.

AGENT-AGNOSTIC: only THIS file is Claude-specific. Another agent (Cursor / Aider / Codex / Cline …) adds
its own parser emitting the SAME `.md` shape into the same pipeline — a parser, not a rewrite.

  python3 src/adapters/sessions.py --out ~/.drillable/sessions \
      [--project bootable-spec] [--since 2026-06-20] [--limit 50]
"""
import argparse
import json
import os
import re
import sys

PROJECTS = os.path.expanduser("~/.claude/projects")
_SYS = re.compile(r"^\s*<(scheduled-task|system-reminder|command-name|command-message|local-command|"
                  r"user-prompt-submit-hook|session-start-hook)\b", re.I)
_WS = re.compile(r"\s+")


def _text(content):
    """Visible text from a message's content (a str, or a list of typed blocks). Thinking is kept
    (capped) — it's the 'what did the agent struggle with' signal; tool RESULTS are dropped (retrieval
    noise); tool USES are noted by name."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            out.append(b.get("text", ""))
        elif t == "thinking":
            th = (b.get("thinking") or "").strip()
            if th:
                out.append("[reasoning] " + th[:800])
        elif t == "tool_use":
            out.append(f"[tool: {b.get('name', '?')}]")
    return "\n".join(x for x in out if x.strip())


def _clean_user(txt):
    """The real human text of a user turn, or '' if it's a system-injected wrapper (scheduled task,
    reminder, hook, command) rather than a person typing."""
    if not txt or _SYS.match(txt):
        return ""
    txt = re.split(r"<system-reminder>", txt, 1)[0]
    return txt.strip()


def turns(path):
    """[(ts, user_text, [assistant_texts])] — a turn = a human message + the assistant text that follows
    until the next human message."""
    out, cur = [], None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            t, msg, ts = d.get("type"), d.get("message"), d.get("timestamp", "")
            if t == "user" and isinstance(msg, dict):
                ut = _clean_user(_text(msg.get("content")))
                if ut:
                    if cur:
                        out.append(cur)
                    cur = {"ts": ts, "user": ut, "asst": []}
            elif t == "assistant" and isinstance(msg, dict) and cur is not None:
                at = _text(msg.get("content"))
                if at:
                    cur["asst"].append(at)
    if cur:
        out.append(cur)
    return out


def session_md(path, project, max_turns=60):
    """The `.md` for one transcript, or None if it has no real human turns."""
    sid = os.path.splitext(os.path.basename(path))[0]
    ta = turns(path)
    if not ta:
        return None
    date = (ta[0]["ts"] or "")[:10] or "unknown"
    topic = _WS.sub(" ", ta[0]["user"]).strip()[:90]
    lines = ["---", "type: session", f"originSessionId: {sid}", f"project: {project}",
             f"date: {date}", "---", "", f"# {date} · {topic}", ""]
    dropped = max(0, len(ta) - max_turns)
    for i, turn in enumerate(ta[:max_turns], 1):
        head = (turn["ts"] or "")[:16].replace("T", " ")
        lines.append(f"## Turn {i} — {head}".rstrip())
        lines.append(f"**User:** {_WS.sub(' ', turn['user']).strip()[:1500]}")
        asst = _WS.sub(" ", " ".join(turn["asst"])).strip()
        if asst:
            lines.append("")
            lines.append(f"**Assistant:** {asst[:2500]}")
        lines.append("")
    if dropped:
        lines.append(f"_({dropped} later turn(s) omitted — this is the head of a long session.)_")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="convert Claude Code transcripts → drillable-context markdown")
    ap.add_argument("--out", required=True, help="output facts_dir (per-project subdirs of <sessionId>.md)")
    ap.add_argument("--projects-dir", default=PROJECTS, help="~/.claude/projects (default)")
    ap.add_argument("--project", help="only slugs containing this substring")
    ap.add_argument("--since", help="only sessions dated >= YYYY-MM-DD (by file mtime)")
    ap.add_argument("--limit", type=int, help="cap the number of sessions (a first-cut slice)")
    a = ap.parse_args()

    src = os.path.expanduser(a.projects_dir)
    if not os.path.isdir(src):
        sys.exit(f"no {src}")
    jobs = []
    for slug in sorted(os.listdir(src)):
        sdir = os.path.join(src, slug)
        if not os.path.isdir(sdir) or (a.project and a.project not in slug):
            continue
        # a project's slug + its worktree slugs share the base project name (…--claude-worktrees-…)
        proj = slug.replace("-Users-jared-Code-", "").split("--claude-worktrees-")[0]
        for fn in sorted(os.listdir(sdir)):
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(sdir, fn)
            if a.since:
                import datetime
                if datetime.date.fromtimestamp(os.path.getmtime(p)).isoformat() < a.since:
                    continue
            jobs.append((p, proj))
    jobs.sort(key=lambda j: os.path.getmtime(j[0]), reverse=True)   # newest first
    if a.limit:
        jobs = jobs[:a.limit]

    out = os.path.expanduser(a.out)
    written = skipped = 0
    for p, proj in jobs:
        md = session_md(p, proj)
        if not md:
            skipped += 1
            continue
        dproj = os.path.join(out, proj)
        os.makedirs(dproj, exist_ok=True)
        sid = os.path.splitext(os.path.basename(p))[0]
        with open(os.path.join(dproj, sid + ".md"), "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
        written += 1
    print(f"sessions → {out}: {written} written · {skipped} empty/skipped (of {len(jobs)} transcripts)")


if __name__ == "__main__":
    main()
