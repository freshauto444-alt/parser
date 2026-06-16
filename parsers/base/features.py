# parsers/base/features.py — Swedish feature translation. Extracted from base.py.

from typing import Optional

from .constants import SV_FEATURES
from .helpers import decode_html

# ══════════════════════════════════════════════════════════════════════════════
#  FEATURE TRANSLATION
# ══════════════════════════════════════════════════════════════════════════════

def translate_feature(raw: str) -> tuple[str, str]:
    cleaned = decode_html(raw.strip())
    key = cleaned.lower()
    if key in SV_FEATURES:
        return SV_FEATURES[key]
    best, best_len = None, 0
    for sv_key, val in SV_FEATURES.items():
        if sv_key in key and len(sv_key) > best_len:
            best, best_len = val, len(sv_key)
    if best:
        return best
    # Keyword categorization for untranslated
    safety_kw = ["abs", "airbag", "lane", "blind", "sensor", "emergency",
                 "collision", "broms", "varning", "kamera", "camera"]
    info_kw = ["navigation", "carplay", "android", "bluetooth", "dab",
               "usb", "wireless", "sound", "navi", "radio", "apple", "cockpit", "screen"]
    comfort_kw = ["heated", "leather", "panorama", "keyless", "climate",
                  "cruise", "värme", "säte", "skinn", "drag", "farthållare"]
    kl = key
    if any(k in kl for k in safety_kw):
        return cleaned, "safety"
    if any(k in kl for k in info_kw):
        return cleaned, "infotainment"
    if any(k in kl for k in comfort_kw):
        return cleaned, "comfort"
    return cleaned, "other"


def translate_and_categorize_features(raw_features: list[str]) -> dict[str, list[str]]:
    result = {"safety": [], "comfort": [], "infotainment": [], "other": []}
    seen: set[str] = set()
    for raw in raw_features:
        if not raw or len(raw.strip()) < 2:
            continue
        en_name, cat = translate_feature(raw)
        key = en_name.lower()
        if key not in seen:
            seen.add(key)
            result[cat].append(en_name)
    return result


