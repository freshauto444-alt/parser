"""
Unit test for the generalized per-trim MOTOR resolution in
parsers/as24_taxonomy.resolve_cat.

The resolver normally lazy-loads its taxonomy from Supabase. To keep this test
offline and deterministic we inject a tiny fake cache straight into the module
globals (_BRANDS / _GROUPS / _MOTORS) and flip _LOADED=True so _ensure_loaded()
short-circuits without touching the network.

Asserts:
  * resolve("bmw", "x5 m")            → x5 group + mt368   (own-group + motor)
  * resolve("mercedes-benz","c 63 amg")→ c-klasse + mt310  (AMG trim path)
  * resolve("mercedes-benz","c63")     → c-klasse + mt310  (c63 ↔ c 63 amg)
  * resolve("bmw", "x5")              → group only, NO motor (no false x5→x5 m)
  * resolve("volkswagen","golf gti")  → group only, NO motor (real data gap)

Run: venv/bin/python tools/check_as24_motor_resolve.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parsers import as24_taxonomy as tax  # noqa: E402


def _install_fake_cache() -> None:
    """Replace the module-level taxonomy cache with a small fixture."""
    tax._BRANDS = {
        "bmw": 9,
        "mercedes-benz": 47,
        "volkswagen": 74,
    }
    # (brand_slug, label_norm) → group_id
    tax._GROUPS = {
        ("bmw", "x5"): 16406,
        ("mercedes-benz", "c-klasse"): 100056,
        ("volkswagen", "golf"): 12345,
    }
    # (brand_slug, group_id) → list of motor rows (mirrors as24_motors columns)
    tax._MOTORS = {
        ("bmw", 16406): [
            {"model_label_norm": "x5", "motortype_id": 353, "listings_count": 8},
            {"model_label_norm": "x5", "motortype_id": 361, "listings_count": 4},
            {"model_label_norm": "x5 m", "motortype_id": 368, "listings_count": 2},
        ],
        ("mercedes-benz", 100056): [
            {"model_label_norm": "c 63 amg", "motortype_id": 310, "listings_count": 16},
            {"model_label_norm": "c 200", "motortype_id": 999, "listings_count": 50},
        ],
        # Golf rows are ALL "golf" upstream — genuine GTI data gap.
        ("volkswagen", 12345): [
            {"model_label_norm": "golf", "motortype_id": 111, "listings_count": 99},
        ],
    }
    tax._LOADED = True


def main() -> int:
    _install_fake_cache()

    checks = [
        # (brand, model, must_contain, must_NOT_contain)
        ("bmw", "x5 m", "ma9gr16406mt368", None),
        ("mercedes-benz", "c 63 amg", "ma47gr100056mt310", None),
        ("mercedes-benz", "c63", "ma47gr100056mt310", None),
        ("bmw", "x5", "ma9gr16406", "mt"),
        ("volkswagen", "golf gti", "ma74gr12345", "mt"),
    ]

    ok = True
    for brand, model, want, forbid in checks:
        got = tax.resolve_cat(brand, model)
        passed = got is not None and want in got
        if forbid is not None and got is not None and forbid in got:
            passed = False
        status = "PASS" if passed else "FAIL"
        if not passed:
            ok = False
        forbid_note = f" (must NOT contain '{forbid}')" if forbid else ""
        print(f"[{status}] resolve({brand!r}, {model!r}) = {got!r} "
              f"— expected to contain {want!r}{forbid_note}")

    print()
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
