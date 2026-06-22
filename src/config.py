#!/usr/bin/env python3
"""config.py — resolve a corpus config from a file OR direct params/env.

Two ways in, so the engine works both standalone and as a Claude Code plugin (whose `.mcp.json` args
are fixed at publish time, so it must pass the user's facts dir as a parameter, not a config path):

  --config <file>                              # standalone: a configs/<name>.json
  --facts-dir <dir> [--name --oracle-repo      # plugin/CLI: direct params (also read from env
                     --standing-types --embed]  #   DRILLABLE_FACTS_DIR / _ORACLE_REPO / _EMBED)

Returns a cfg dict with every field the engine reads, plus `_dir` (where the DB lives) and `_db`.
In param/env mode the DB lives under ~/.drillable-context/ (a stable per-user spot, since the plugin
install dir may be read-only or replaced on update).
"""
import argparse
import json
import os

HOME = os.path.expanduser("~/.drillable-context")


def resolve(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--config")
    ap.add_argument("--facts-dir")
    ap.add_argument("--name", default="context")
    ap.add_argument("--oracle-repo")
    # Default to "preference" so a one-click plugin / bare-CLI install (whose .mcp.json passes no
    # --standing-types) still classifies preference facts as standing — otherwise context_standing
    # always returns nothing. Pass --standing-types "" to opt out, or a CSV to set your own types.
    ap.add_argument("--standing-types", default="preference")
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--db")
    a, _ = ap.parse_known_args(argv)

    if a.config:
        p = os.path.abspath(a.config)
        if not os.path.exists(p):
            raise SystemExit(f"config not found: {p}")
        cfg = json.load(open(p, encoding="utf-8"))
        cfg.setdefault("name", "context")
        cfg["_dir"] = os.path.dirname(p)
        cfg["facts_dir"] = os.path.abspath(os.path.join(cfg["_dir"], cfg["facts_dir"]))
        cfg["_db"] = a.db or os.path.join(cfg["_dir"], f"{cfg['name']}.db")
        return cfg

    facts_dir = a.facts_dir or os.environ.get("DRILLABLE_FACTS_DIR")
    if facts_dir:
        os.makedirs(HOME, exist_ok=True)
        cfg = {
            "name": a.name,
            "facts_dir": os.path.abspath(os.path.expanduser(facts_dir)),
            "oracle_repo": a.oracle_repo or os.environ.get("DRILLABLE_ORACLE_REPO") or None,
            "standing_types": [s for s in (a.standing_types or "").split(",") if s],
            "type_field": "type", "index_file": None, "recursive": True, "exclude": [],
            "embed": a.embed or os.environ.get("DRILLABLE_EMBED") in ("1", "true"),
            "_dir": HOME,
        }
        cfg["_db"] = a.db or os.path.join(HOME, f"{a.name}.db")
        return cfg

    raise SystemExit("provide --config <file> or --facts-dir <dir> (or env DRILLABLE_FACTS_DIR)")
