#!/usr/bin/env python3
"""Unit test for the orchestrator performance-trim model gate.

Faithfully replicates _model_matches (orchestrator.py) so we can assert that a
performance-trim query ("X5 M", "Golf GTI", "i30 N") returns ONLY the real trim
and rejects the base model + wrong-base trims — the reported bug was the parser
returning 47 base "X5" + 1 real "X5 M". Run: python tools/check_model_matches.py
"""
from __future__ import annotations
import re
import sys

_PERF_TRIMS = {"gti", "gtd", "amg", "rs", "gts", "competition", "jcw",
               "cupra", "abarth", "quadrifoglio", "m"}


def model_matches(wanted_model: str, c_model: str) -> bool:
    wanted_model = (wanted_model or "").strip().lower()
    req_tokens = {t for t in re.split(r"[\s\-]+", wanted_model)
                  if t and t not in ("series", "class", "klasse")}
    wanted_trims = req_tokens & _PERF_TRIMS
    cm = (c_model or "").lower().strip()
    if not cm:
        return True
    if wanted_trims:
        cm_tok = {t for t in re.split(r"[\s\-]+", cm) if t}
        if not wanted_trims.issubset(cm_tok):
            return False
        base_tokens = req_tokens - wanted_trims
        if base_tokens and not (base_tokens & cm_tok):
            return False
        return True
    if wanted_model in cm or cm in wanted_model:
        return True
    # series digit
    if "series" in wanted_model or wanted_model.isdigit() or re.match(r"^\d+(er)?$", wanted_model):
        dm = re.match(r"^(\d)", wanted_model)
        if dm:
            d = dm.group(1)
            if cm.startswith("x"):
                return False
            mm = re.match(r"^[mM]?(\d)", cm)
            return bool(mm and mm.group(1) == d)
    cm_tokens = {t for t in re.split(r"[\s\-]+", cm) if t}
    return bool(req_tokens & cm_tokens)


CASES = [
    # (wanted, car, expected)
    ("X5 M", "X5", False), ("X5 M", "X5 M", True),
    ("X5 M", "X5 M Competition", True), ("X5 M", "X5 M50d", False),
    ("X5 M", "X4 M", False), ("X5 M", "X5 xDrive40d", False),
    ("Golf GTI", "Golf", False), ("Golf GTI", "Golf GTI", True),
    ("Golf GTI", "Golf-Serie", False), ("Golf GTI", "Golf Variant", False),
    ("Golf GTI", "Polo GTI", False), ("Golf GTI", "Golf GTI Performance", True),
    # Single-letter n/r are intentionally NOT gated (sources don't label them in
    # `model` → gating would zero real trims). Base matches via the loose default;
    # precise targeting is a source-level (model/motor code) job, not this filter.
    ("i30 N", "i30 N", True),
    ("Golf R", "Golf R", True),
    ("Octavia RS", "Octavia", False), ("Octavia RS", "Octavia RS", True),
    ("C 63 AMG", "C-Klass", False), ("C 63 AMG", "C 63 AMG", True),
    # Non-trim queries must still behave (no gate):
    ("X5", "X5", True), ("X5", "X5 M", True), ("X5", "X3", False),
    ("3 series", "320", True), ("3 series", "M3", True), ("3 series", "X3", False),
    ("Octavia", "Octavia", True), ("Octavia", "Octavia RS", True),
    ("Passat", "Passat Variant", True), ("Passat", "Golf", False),
]


def main() -> int:
    fails = []
    for wanted, car, exp in CASES:
        got = model_matches(wanted, car)
        if got != exp:
            fails.append(f"  model_matches({wanted!r}, {car!r}) = {got}, expected {exp}")
    if fails:
        print("FAILED:\n" + "\n".join(fails))
        return 1
    print(f"model-match gate: all {len(CASES)} cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
