# parsers/parser_sweden/blocket_variants.py
# Resolve a (make_code, model) pair to Blocket's deepest matching `variant` code.
#
# Blocket's search API takes ONE `variant` param at any depth (make / series /
# model-trim). Sending the deepest code that matches the requested model is what
# fixes base-model pollution: free-text `q="X5 M"` still returns base X5 cars,
# but `variant=2.749.2466.8360` ("X5 M") does not. The lookup table is built
# offline by tools/build_blocket_variants.py from the harvested facet taxonomy
# and committed as blocket_variants.json.
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from loguru import logger

_DATA_PATH = Path(__file__).with_name("blocket_variants.json")


@lru_cache(maxsize=1)
def _variants() -> dict[str, dict[str, str]]:
    """Load { make_code: { normalized_model: variant_code } } once (cached)."""
    try:
        return json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except Exception as e:  # pragma: no cover — missing/corrupt data is non-fatal
        logger.warning(f"[blocket:variants] could not load {_DATA_PATH.name}: {e}")
        return {}


def _normalize(name: str) -> str:
    """Same reduction as the build step: lowercase, punctuation→space, collapse.

    Kept in lock-step with tools/build_blocket_variants.normalize so a model
    string and a stored key reduce identically.
    """
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def resolve_variant(make_code: str, model: str) -> Optional[str]:
    """Deepest Blocket `variant` code for `model` within `make_code`, or None.

    Matching strategy (most-specific wins), all scoped to the one make:
      1. Exact normalized match — both a spaced ("x5 m") and an unspaced ("x5m")
         key were emitted at build time, so "X5 M"/"X5M" both hit directly.
      2. Series fallback for a bare model name: many Blocket series are stored as
         "<model> serie" ("Golf-Serie"→"golf serie"). If "<query> serie" exists
         we use that series node (so "Golf" still narrows to the Golf series).
      3. Longest stored key that is a whole-token PREFIX of the query — handles
         "x5 m sport" → "x5 m". We require the boundary to fall on a token so
         "x5" does not swallow "x50".
    Deliberately NOT a loose "contains" match: a query that names a trim absent
    from the tree (e.g. "Golf GTI") must return None so the caller can fall back
    to free-text + a post-filter, rather than silently widening to base Golf.
    """
    if not make_code or not model:
        return None
    bucket = _variants().get(make_code)
    if not bucket:
        return None

    q = _normalize(model)
    if not q:
        return None

    # 1) exact
    if q in bucket:
        return bucket[q]
    qx = q.replace(" ", "")
    if qx in bucket:
        return bucket[qx]

    # 2) "<model> serie" series node (Blocket names series "<X>-Serie")
    serie = bucket.get(f"{q} serie") or bucket.get(f"{qx}serie")
    if serie:
        return serie

    # 2b) leading-token prefixes joined unspaced — bridges the spacing mismatch
    #     between our input ("C 63 AMG") and Blocket's unspaced trim names ("C63").
    #     Try the longest leading run first: "c63amg"→"c63"→"c", taking the first
    #     exact key hit (so "C 63 AMG" lands on the "c63" trim node).
    q_tokens = q.split(" ")
    if len(q_tokens) > 1:
        for n in range(len(q_tokens), 0, -1):
            joined = "".join(q_tokens[:n])
            code = bucket.get(joined)
            if code:
                return code

    # 3) longest stored key that is a token-boundary prefix of the query
    best_code: Optional[str] = None
    best_len = 0
    for key, code in bucket.items():
        if " " not in key:
            continue  # unspaced duplicates handled via the spaced form
        k_tokens = key.split(" ")
        n = len(k_tokens)
        if n <= best_len:
            continue
        if q_tokens[:n] == k_tokens:  # key is a leading token-run of the query
            best_code, best_len = code, n
    return best_code
