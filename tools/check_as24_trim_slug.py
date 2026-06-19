#!/usr/bin/env python3
"""Unit test for AS24 perf-trim slug preference (_prefers_trim_slug + build_as24_url).

A perf-trim query ("Golf GTI") that only resolves to a base GROUP cat
(cat=ma74gr100101 = all 9564 Golf) must instead use the trim PATH slug
(/volkswagen/golf-gti = 1032 pure GTI). Cars with a precise motor cat (mt…, e.g.
X5 M / C 63 AMG) must keep the cat. Run: python tools/check_as24_trim_slug.py
"""
from __future__ import annotations
import sys
from parsers.as24_http import _prefers_trim_slug, build_as24_url

CASES = [  # (model, cat, expected_prefers_slug)
    ("Golf GTI", "ma74gr100101", True),            # trim + base group → slug
    ("Golf GTI", "ma74gr100101mt436", False),      # precise motor cat → keep
    ("Golf R", "ma74gr100101", True),
    ("Polo GTI", "ma15gr200", True),
    ("i30 N", "ma47gr19065", True),
    ("Golf", "ma74gr100101", False),               # no trim token
    ("Passat Variant", "ma74gr100100", False),     # "variant" is a body, not a perf trim
    ("X5 M", "ma9gr16406mt368", False),            # precise motor cat → keep
    ("Golf GTI", None, False),                      # no cat → nothing to override
    ("Golf GTI", "", False),
]


def main() -> int:
    fails = []
    for model, cat, exp in CASES:
        got = _prefers_trim_slug(model, cat)
        if got != exp:
            fails.append(f"  _prefers_trim_slug({model!r}, {cat!r}) = {got}, expected {exp}")

    # build_as24_url with prefer_slug=True must slugify "Golf GTI" → golf-gti path
    url = build_as24_url({"brand": "Volkswagen", "model": "Golf GTI", "year_from": 2021},
                         prefer_slug=True)
    if "/volkswagen/golf-gti" not in url:
        fails.append(f"  build_as24_url slug path wrong: {url}")

    if fails:
        print("FAILED:\n" + "\n".join(fails)); return 1
    print(f"as24-trim-slug: all {len(CASES) + 1} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
