#!/usr/bin/env python3
"""Unit test for the spec/hp-aware trim refinement (orchestrator).

Replicates _nr_ok (N/R hot-hatch vs N-Line/base) and _xm_ok (full X-M vs M50d
diesel) so we can assert the criterion-1 fix: an "i30 N" search returns the real
hot-hatch (≥240hp / "N Performance"), not base i30 nor the N-Line package; an
"X5 M" search drops the 400hp X5 M50d diesel. Run: python tools/check_trim_spec_filter.py
"""
from __future__ import annotations
import re, sys
from types import SimpleNamespace as C


def _perf_text(c) -> str:
    return " ".join(filter(None, [c.model, c.title_line, c.engine])).lower()

def _displacement(engine):
    if not engine:
        return None
    m = re.search(r"(\d\.\d)", engine)
    return float(m.group(1)) if m else None

def nr_ok(c, nr_trims) -> bool:
    text = _perf_text(c); hp = c.horsepower; disp = _displacement(c.engine)
    for t in nr_trims:
        stripped = re.sub(rf"\b{t}[\s\-]?line\b", " ", text)
        strong = (f"{t} performance" in text
                  or (hp is not None and hp >= 240)
                  or (disp is not None and disp >= 1.9 and re.search(rf"\b{t}\b", stripped) is not None))
        if hp is None and disp is None:
            strong = strong or bool(re.search(rf"\b{t}\b", stripped))
        if not strong:
            return False
    return True

def xm_ok(c) -> bool:
    cm = (c.model or "").lower()
    if re.search(r"\bm\b", cm) or "competition" in _perf_text(c):
        return True
    hp = c.horsepower
    return hp is None or hp >= 460

def car(model="", hp=None, engine=None, title=None):
    return C(model=model, horsepower=hp, engine=engine, title_line=title)

NR = [  # (car, keep?)
    (car("I30", 120, "1.0L", "1.4 T-GDi Comfort"), False),            # base
    (car("I30", 160, "1.5L", "1.5 T-GDI 160hk N-Line Carplay"), False), # N-Line package
    (car("I30", 120, "1.0L", "1.0 T-GDI 120hk N-Line B-Kamera"), False),
    (car("I30", 275, "2.0L", "N Performance 275HK GT-Paket"), True),  # real N
    (car("I30", 280, "2.0L", "N Performance DCT (280hk) NAVI"), True),# real N
    (car("I30", None, None, "Hyundai i30 N Performance"), True),      # no hp, perf text
    (car("I30", None, None, "Hyundai i30 N-Line"), False),           # no hp, N-Line only
]
NR_TRIMS = {"n"}

R = [  # Golf R, nr_trims={"r"}
    (car("Golf R", 300, "2.0L", "Golf R 4Motion DSG"), True),
    (car("Golf", 110, "1.0L", "Golf 1.0 TSI Comfort"), False),
    (car("Golf", 150, "1.5L", "Golf R-Line 1.5 TSI"), False),         # R-Line package
]
R_TRIMS = {"r"}

XM = [  # (car, keep?)
    (car("X5 M", 575, "4.4L", "BMW X5 M"), True),                     # labelled
    (car("X5", 400, "3.0 Diesel", "BMW X5"), False),                 # M50d diesel leak
    (car("X5", 381, "3.0 Diesel", "BMW X5"), False),
    (car("X5", 617, "4.4L", "BMW X5 M Competition"), True),          # spec says Competition
    (car("X5", None, None, "BMW X5"), True),                        # unknown hp → conservative keep
]

def main() -> int:
    fails = []
    for c, exp in NR:
        if nr_ok(c, NR_TRIMS) != exp:
            fails.append(f"  nr_ok({c.model!r},{c.horsepower},{c.title_line!r})={not exp}, want {exp}")
    for c, exp in R:
        if nr_ok(c, R_TRIMS) != exp:
            fails.append(f"  nr_ok/R({c.title_line!r})={not exp}, want {exp}")
    for c, exp in XM:
        if xm_ok(c) != exp:
            fails.append(f"  xm_ok({c.model!r},{c.horsepower})={not exp}, want {exp}")
    if fails:
        print("FAILED:\n" + "\n".join(fails)); return 1
    print(f"trim-spec filter: all {len(NR)+len(R)+len(XM)} cases passed.")
    return 0

sys.exit(main())
