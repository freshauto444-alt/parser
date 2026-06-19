#!/usr/bin/env python3
"""Unit test for bytbil_freetext() — the safe Bytbil FreeText deriver.

Bytbil's FreeText only matches a SINGLE model token as a prefix; a space or a
bare short token returns ZERO (live-probed 2026-06-19), while Makes-only always
returns a full page. So multi-word/trim and bare-digit models must NOT be passed
verbatim — they collapsed that source to 0 (the SOURCE_0 class the pipeline
detector flagged on 6 of 11 targets). This guards the derivation.

Run:  venv/bin/python tools/check_bytbil_freetext.py   (exit 1 on any failure)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parsers.parser_sweden.bytbil import bytbil_freetext  # noqa: E402

# model (as parse_bytbil sees it, already .title()'d) -> expected FreeText
CASES = [
    # single usable token → kept (these probed non-zero on Bytbil)
    ("Golf", "Golf"),
    ("I30", "I30"),
    ("M240I", "M240I"),
    ("M340I", "M340I"),
    ("Rs6", "Rs6"),
    ("Mazda6", "Mazda6"),
    # multi-word / trim → only the base token (the full word collapsed to 0)
    ("Golf R", "Golf"),
    ("I30 N", "I30"),
    ("M340I Xdrive", "M340I"),
    ("Golf Gti", "Golf"),
    # bare/short/separator tokens → empty (would zero Bytbil); rely on slug filter
    ("6", ""),
    ("3 Series", ""),       # first token "3" too short
    ("C-Class", ""),        # hyphen → not alnum
    ("A4", ""),             # len 2 → slug filter handles "a4"
    ("", ""),
]


def main() -> int:
    failures = 0
    for model, expected in CASES:
        got = bytbil_freetext(model)
        ok = got == expected
        status = "ok " if ok else "FAIL"
        print(f"  [{status}] bytbil_freetext({model!r}) = {got!r}  (want {expected!r})")
        if not ok:
            failures += 1
    print()
    if failures:
        print(f"bytbil_freetext check FAILED — {failures} case(s).")
        return 1
    print(f"bytbil_freetext check passed — {len(CASES)} cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
