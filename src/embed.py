#!/usr/bin/env python3
"""embed.py — optional embedding retriever (mirrors the gateway's `nearest.ts`).

Stdlib-only (urllib) so the no-pip-install property survives. Calls OpenAI's embeddings endpoint with
`text-embedding-3-small` — the same model the drillable gateway uses (F2dl proved recall@3 = 100%,
scale-invariant to 25×). Degrades gracefully: no key → returns None and the engine falls back to the
keyword scorer (so a small/offline corpus still works with zero config).

The model + endpoint are grounded in the gateway's working retriever, not memory — drillable's `models`
domain honestly abstains on OpenAI model specs (a declared vendor boundary), so we don't assert
dimensions; we store whatever the API returns.
"""
import json
import math
import os
import urllib.request

ENDPOINT = "https://api.openai.com/v1/embeddings"
MODEL = os.environ.get("DRILLABLE_EMBED_MODEL", "text-embedding-3-small")


def _key():
    return os.environ.get("OPENAI_API_KEY")


def available():
    return _key() is not None


def embed(texts, batch=100):
    """List[str] -> List[vector], or None if no key. Order-preserving (sorts by response index)."""
    key = _key()
    if not key:
        return None
    out = []
    for i in range(0, len(texts), batch):
        chunk = [(t[:8000] or " ") for t in texts[i:i + batch]]
        req = urllib.request.Request(
            ENDPOINT, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            data=json.dumps({"model": MODEL, "input": chunk}).encode())
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        out.extend(d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"]))
    return out


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
