#!/usr/bin/env python3
"""test_sessions — the Claude Code sessions adapter (src/adapters/sessions.py): a synthetic transcript
converts to engine-ready markdown (frontmatter type:session + originSessionId → provenance; ## Turn
sections), system-injected user wrappers are dropped, and tool RESULTS are dropped while thinking is kept.
Run: python3 test_sessions.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "adapters"))
import sessions  # noqa: E402


def _rec(t, content, ts="2026-06-30T12:00:00Z"):
    return json.dumps({"type": t, "timestamp": ts, "message": {"role": t, "content": content}})


class TestSessionsAdapter(unittest.TestCase):
    def _write(self, lines):
        fd, p = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return p

    def test_turns_and_frontmatter(self):
        p = self._write([
            _rec("user", "how do I convert km to miles?"),
            _rec("assistant", [{"type": "thinking", "thinking": "they want a unit conversion"},
                               {"type": "text", "text": "Use the convert verb."}]),
            _rec("user", "and the contested case?"),
            _rec("assistant", [{"type": "text", "text": "It forks per reading."}]),
        ])
        md = sessions.session_md(p, "myproject")
        self.assertIn("type: session", md)
        self.assertIn("originSessionId:", md)
        self.assertIn("project: myproject", md)
        self.assertEqual(md.count("## Turn "), 2)                 # two human turns → two chunks
        self.assertIn("**User:** how do I convert km to miles?", md)
        self.assertIn("Use the convert verb.", md)
        self.assertIn("[reasoning] they want a unit conversion", md)   # thinking kept (the struggle signal)
        os.unlink(p)

    def test_system_wrappers_and_tool_results_dropped(self):
        p = self._write([
            _rec("user", "<scheduled-task name=\"x\">a system-injected turn</scheduled-task>"),
            _rec("assistant", [{"type": "text", "text": "should attach to the NEXT real turn"}]),
            _rec("user", "a real question"),
            _rec("user", [{"type": "tool_result", "content": "TOOL OUTPUT NOISE"}]),  # not a human turn
            _rec("assistant", [{"type": "tool_use", "name": "Bash"},
                               {"type": "text", "text": "the answer"}]),
        ])
        md = sessions.session_md(p, "p")
        self.assertEqual(md.count("## Turn "), 1)                 # only the ONE real human turn
        self.assertIn("a real question", md)
        self.assertNotIn("system-injected", md)                  # the scheduled-task wrapper dropped
        self.assertNotIn("TOOL OUTPUT NOISE", md)                # tool results dropped
        self.assertIn("[tool: Bash]", md)                        # tool use noted by name
        os.unlink(p)

    def test_empty_transcript_returns_none(self):
        p = self._write([_rec("user", "<system-reminder>only system</system-reminder>")])
        self.assertIsNone(sessions.session_md(p, "p"))
        os.unlink(p)


class TestIncrementalConvert(unittest.TestCase):
    """The 'updated user path' core: convert() is INCREMENTAL — a re-run touches only NEW/changed
    sessions (fresh=0 when nothing changed), so the server's throttled auto-convert is cheap and only a
    genuinely-new session triggers a reseed."""

    def _projects(self):
        d = tempfile.mkdtemp(prefix="proj-")
        sdir = os.path.join(d, "-Users-x-Code-demo")
        os.makedirs(sdir)
        return d, sdir

    def _session(self, sdir, sid, text):
        with open(os.path.join(sdir, sid + ".jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": "2026-06-30T12:00:00Z",
                                 "message": {"role": "user", "content": text}}) + "\n")

    def test_incremental_skips_unchanged_then_picks_up_new(self):
        proj, sdir = self._projects()
        out = tempfile.mkdtemp(prefix="out-")
        self._session(sdir, "s1", "first session about widgets")

        r1 = sessions.convert(out, proj)
        self.assertEqual(r1["fresh"], 1)                 # first run converts it

        r2 = sessions.convert(out, proj)
        self.assertEqual(r2["fresh"], 0)                 # nothing changed → no rewrite (cheap re-run)
        self.assertEqual(r2["written"], 1)

        self._session(sdir, "s2", "a new session about sprockets")
        r3 = sessions.convert(out, proj)
        self.assertEqual(r3["fresh"], 1)                 # only the NEW session is (re)written
        self.assertEqual(r3["written"], 2)
        self.assertTrue(os.path.exists(os.path.join(out, "demo", "s2.md")))  # emitted under its project


if __name__ == "__main__":
    unittest.main()
