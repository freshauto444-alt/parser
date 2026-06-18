#!/usr/bin/env python3
"""Build a committed Blocket variant lookup from the sampled facet taxonomy.

Blocket's search API accepts a single `variant` param at every depth: a make
code (`0.749` = BMW), a series code (`1.749.2466` = X-Serie), or a model/trim
code (`2.749.2466.8360` = "X5 M"). Sending the deepest code that matches the
requested model is what stops base-model pollution (free-text `q=X5 M` still
returns base X5 cars; `variant=2.749.2466.8360` does not).

This reads `blocket_sample.json` → `filters[name=="variant"].filter_items`
(144 makes, ~2.8k nodes, 3 levels) and flattens it into:

    { make_code: { normalized_model_name: variant_code } }

keyed by make code (so the runtime can scope the lookup to the brand already
resolved by `_blocket_make_code`). Series-level and model/trim-level nodes are
both included, so "x5" resolves to the X5 model node and "x5 m" to the X5 M
trim node. Both spaced and unspaced normalized keys are emitted where they
differ ("x5 m" and "x5m", "c63" and "c 63") so model strings that arrive with
or without internal spaces still hit.

Output: parsers/parser_sweden/blocket_variants.json (committed, no network).
Run:  venv/bin/python tools/build_blocket_variants.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "blocket_sample.json"
OUT = ROOT / "parsers" / "parser_sweden" / "blocket_variants.json"


def normalize(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Matches how a model string is reduced before lookup. Keeps internal single
    spaces ("c 63", "x5 m"); the caller separately emits an unspaced variant.
    "C-Klass (W205)" → "c klass w205"; "X5 M" → "x5 m"; "Cayman GT4" → "cayman gt4".
    """
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)  # punctuation/hyphens → space
    return re.sub(r"\s+", " ", s).strip()


def _find_variant_filter(obj) -> dict | None:
    """Locate the `name=="variant"` facet anywhere in the sampled response."""
    if isinstance(obj, dict):
        for f in obj.get("filters", []) or []:
            if isinstance(f, dict) and f.get("name") == "variant":
                return f
        for v in obj.values():
            r = _find_variant_filter(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_variant_filter(v)
            if r:
                return r
    return None


def build() -> dict[str, dict[str, str]]:
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    vf = _find_variant_filter(data)
    if not vf:
        raise SystemExit("no `variant` filter found in blocket_sample.json")

    out: dict[str, dict[str, str]] = {}
    nodes = 0

    def add_key(make_code: str, key: str, code: str) -> None:
        """Register key → code for a make, without clobbering a deeper code.

        Codes are dotted; depth == number of dots. A series-level node ("X-Serie"
        → "x serie") and a model-level node never share a key, but if two nodes
        DID normalize to the same key we keep the deeper (more specific) one.
        """
        if not key:
            return
        bucket = out.setdefault(make_code, {})
        prev = bucket.get(key)
        if prev is None or code.count(".") > prev.count("."):
            bucket[key] = code

    def walk(make_code: str, node: dict) -> None:
        nonlocal nodes
        for child in node.get("filter_items", []) or []:
            nodes += 1
            disp = child.get("display_name") or ""
            code = child.get("value") or ""
            spaced = normalize(disp)          # "x5 m"
            unspaced = spaced.replace(" ", "")  # "x5m"
            if code and "." in code:           # skip the make root itself
                add_key(make_code, spaced, code)
                if unspaced != spaced:
                    add_key(make_code, unspaced, code)
            walk(make_code, child)

    for make in vf.get("filter_items", []) or []:
        make_code = make.get("value") or ""
        if not make_code:
            continue
        walk(make_code, make)

    print(f"makes={len(out)} model_keys={sum(len(v) for v in out.values())} nodes_seen={nodes}")
    return out


def main() -> None:
    mapping = build()
    OUT.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=0, sort_keys=True),
        encoding="utf-8",
    )
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
