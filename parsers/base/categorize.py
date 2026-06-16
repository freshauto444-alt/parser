# parsers/base/categorize.py — feature categorisation + gallery dedup. From base.py.

import re
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
#  COUNTRY CODE MAP (canonical — import this, don't duplicate)
# ══════════════════════════════════════════════════════════════════════════════

COUNTRY_CODE_MAP = {
    "DE": "Germany", "NL": "Netherlands", "BE": "Belgium",
    "AT": "Austria", "FR": "France", "IT": "Italy",
    "ES": "Spain", "CH": "Switzerland", "PL": "Poland",
    "CZ": "Czech Republic", "DK": "Denmark", "SE": "Sweden",
    "NO": "Norway", "FI": "Finland", "GB": "UK", "IE": "Ireland",
    "PT": "Portugal", "LU": "Luxembourg", "HU": "Hungary",
    "RO": "Romania", "HR": "Croatia", "SI": "Slovenia",
    "SK": "Slovakia", "BG": "Bulgaria", "LT": "Lithuania",
    "LV": "Latvia", "EE": "Estonia",
}


def categorize_feature(name: str) -> str:
    """Categorize a feature by keyword when not in SV_FEATURES. Canonical function."""
    nl = name.lower()
    safety_kw = ["abs", "airbag", "lane", "blind", "sensor", "emergency",
                 "collision", "broms", "varning", "esp", "camera", "braking"]
    info_kw = ["navigation", "carplay", "android", "bluetooth", "dab",
               "usb", "wireless", "sound", "navi", "radio", "apple",
               "cockpit", "screen", "display", "infotainment"]
    comfort_kw = ["heated", "leather", "panorama", "keyless", "climate",
                  "cruise", "värme", "säte", "skinn", "drag", "farthållare",
                  "massage", "ventilated", "seat", "steering"]
    if any(k in nl for k in safety_kw):
        return "safety"
    if any(k in nl for k in info_kw):
        return "infotainment"
    if any(k in nl for k in comfort_kw):
        return "comfort"
    return "other"


# ══════════════════════════════════════════════════════════════════════════════
#  GALLERY DEDUP
# ══════════════════════════════════════════════════════════════════════════════

def deduplicate_gallery(images: list[str]) -> list[str]:
    """Remove duplicate photo URLs from gallery."""
    seen = set()
    result = []
    for img in images:
        if img and img not in seen:
            seen.add(img)
            result.append(img)
    return result


