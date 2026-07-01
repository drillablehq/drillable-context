#!/usr/bin/env python3
"""test_project_scope — the session-corpus project filter (server.v_search project=): a query scoped to
one project returns only that project's facts (no cross-project contamination), 'all' spans + labels every
project, an explicit miss abstains honestly, and an implicit-default miss falls through to all (never a
silent total-miss). Network-free: embed:false → keyword retrieval. Run: python3 test_project_scope.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
sys.path.insert(0, SRC)

_MD = ("---\ntype: session\noriginSessionId: {sid}\nproject: {proj}\ndate: 2026-06-30\n---\n"
       "# {proj} session\n\n## Turn 1 — 2026-06-30 12:00\n**User:** zebrafish quantum widget in {proj}\n")


class TestProjectScope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="ctx-scope-")
        facts = os.path.join(cls.dir, "facts")
        for proj in ("alpha", "beta"):
            d = os.path.join(facts, proj)
            os.makedirs(d)
            with open(os.path.join(d, f"s-{proj}.md"), "w") as fh:
                fh.write(_MD.format(sid=f"sess-{proj}", proj=proj))
        cls.cfg_path = os.path.join(cls.dir, "cfg.json")
        with open(cls.cfg_path, "w") as fh:
            json.dump({"name": "scopetest", "facts_dir": facts, "oracle_repo": None,
                       "standing_types": [], "type_field": "type", "recursive": True, "embed": False}, fh)
        # seed once (embed:false → no network); the schema carries the new project column
        subprocess.run([sys.executable, os.path.join(SRC, "seed.py"), "--config", cls.cfg_path],
                       check=True, capture_output=True)
        import server
        cls.server = server
        cls.cfg = server.load_cfg(["--config", cls.cfg_path])

    def _projects(self, res):
        return sorted({ln.split("·", 1)[1].split()[0] for ln in res.splitlines()
                       if ln.strip().startswith("(") and "·" in ln})

    def _q(self, **kw):
        return self.server.v_search(self.cfg, "zebrafish quantum widget", **kw)

    def test_scoped_to_one_project(self):
        res = self._q(project="alpha")
        self.assertEqual(self._projects(res), ["alpha"])          # NO beta contamination
        self.assertIn("scoped to project \"alpha\"", res)

    def test_all_spans_and_labels(self):
        res = self._q(project="all")
        self.assertEqual(self._projects(res), ["alpha", "beta"])  # both, each labelled
        self.assertIn("spanning 2 projects", res)

    def test_explicit_miss_abstains(self):
        self.assertIn("no facts in project", self._q(project="nope-xyz"))

    def test_implicit_default_miss_falls_through_to_all(self):
        # cwd here is not 'alpha'/'beta' → the implicit default matches nothing → must show ALL, never blank
        res = self._q()
        self.assertEqual(self._projects(res), ["alpha", "beta"])
        self.assertNotIn("no facts in project", res)


if __name__ == "__main__":
    unittest.main()
