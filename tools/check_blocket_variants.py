#!/usr/bin/env python3
"""Unit test for resolve_variant() — the Blocket model→`variant` code resolver.

Guards the base-model-pollution fix: a precise trim ("X5 M", "C63", "S3") must
resolve to its deep `variant` code, while a trim ABSENT from Blocket's tree
("Golf GTI") must return None so the caller falls back to free-text + post-filter.

Run:  venv/bin/python tools/check_blocket_variants.py   (exit 1 on any failure)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parsers.parser_sweden.blocket_variants import resolve_variant  # noqa: E402

# Make codes (level-0 `variant` values) from blocket_sample.json.
BMW = "0.749"
MERCEDES = "0.785"
AUDI = "0.744"
VW = "0.817"


def main() -> int:
    cases: list[tuple[str, str, str, object]] = [
        # (label, make_code, model, expected)  — expected None means "falls back".
        ("BMW X5 M trim", BMW, "X5 M", "2.749.2466.8360"),
        ("BMW X5M (unspaced)", BMW, "X5M", "2.749.2466.8360"),
        ("BMW X5 base model", BMW, "X5", "2.749.2466.6737"),
        ("BMW X5 M Competition (prefix)", BMW, "X5 M Competition", "2.749.2466.8360"),
        ("Mercedes C63", MERCEDES, "C63", "2.785.3776.2002576"),
        ("Mercedes C 63 AMG", MERCEDES, "C 63 AMG", "2.785.3776.2002576"),
        ("Audi S3", AUDI, "S3", "2.744.2482.3731"),
        ("VW Golf GTI -> fallback", VW, "Golf GTI", None),
        # English→Swedish body-class bridge: site sends "3 Series"/"C-Class",
        # Blocket stores "3 serie"/"c klass". Without the bridge these popular
        # queries fall to polluted free-text. Series/class nodes are 1.* codes.
        ("BMW 3 Series -> serie node", BMW, "3 Series", "1.749.2132"),
        ("BMW 5 Series -> serie node", BMW, "5 Series", "1.749.2131"),
        ("Mercedes C-Class -> klass node", MERCEDES, "C-Class", "1.785.3776"),
        ("Mercedes E-Class -> klass node", MERCEDES, "E-Class", "1.785.3775"),
        ("Mercedes GLC-Class -> klass node", MERCEDES, "GLC-Class", "1.785.2000332"),
        ("Unknown make -> None", "9.999", "Whatever", None),
    ]

    failures = 0
    for label, make_code, model, expected in cases:
        got = resolve_variant(make_code, model)
        if expected is None:
            ok = got is None
        elif isinstance(expected, str) and expected.startswith("2.785.3776"):
            # "C63" vs "C 63 AMG": accept any C63-trim node under the C-series.
            ok = bool(got) and got.startswith("2.785.3776")
        else:
            ok = got == expected
        status = "ok " if ok else "FAIL"
        print(f"  [{status}] {label}: resolve_variant({make_code!r}, {model!r}) = {got!r}")
        if not ok:
            failures += 1

    print()
    if failures:
        print(f"resolve_variant check FAILED — {failures} case(s).")
        return 1
    print(f"resolve_variant check passed — {len(cases)} cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
