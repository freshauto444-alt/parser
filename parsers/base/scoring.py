# parsers/base/scoring.py — listing score + premium-feature counting. From base.py.

from datetime import datetime, timedelta, timezone
from typing import Optional

def calc_score(
    year: Optional[int] = None,
    mileage: Optional[int] = None,
    price_eur: Optional[float] = None,
    has_image: bool = False,
    has_features: bool = False,
    hp: Optional[int] = None,
    gallery_count: int = 0,
    feature_count: int = 0,
    has_color: bool = False,
    has_drive: bool = False,
    has_body_type: bool = False,
    # Quality signals (new — optional, pass when parser has extracted them)
    previous_owners: Optional[int] = None,
    accident_free: Optional[bool] = None,
    service_history: Optional[bool] = None,
    has_damage: Optional[bool] = None,
    warranty_months: Optional[int] = None,
    seller_type: Optional[str] = None,
    premium_feature_count: int = 0,
) -> int:
    """
    Ranks a listing 0-100 by how "good a buy" it is.

    Higher is better. Components:
      • Freshness (year)             up to +15
      • Mileage                      up to +20 (with suspicious-low penalty)
      • No accidents                 +12
      • Full service history         +8
      • 1-owner                      +5 (3+ owners: −8)
      • Dealer seller                +3
      • Remaining warranty           up to +5
      • Valid inspection (TÜV)       +3
      • Premium features             up to +10
      • Feature richness total       up to +10
      • Power band                   up to +5
      • Data completeness            up to +10
    """
    # Base score 30 so typical cars sit at ~55-75; only exceptional listings hit 95+.
    # This gives meaningful ranking spread instead of everyone hitting 100.
    score = 30

    # ── Critical: damaged cars always rank last ─────────────────────────────
    if has_damage is True:
        return 5  # not 0 — still allow display, but always at bottom

    # ── Freshness (max +12) ────────────────────────────────────────────────
    if year:
        if year >= 2024: score += 12
        elif year >= 2022: score += 9
        elif year >= 2020: score += 6
        elif year >= 2018: score += 3
        elif year < 2014: score -= 5

    # ── Mileage (max +18, with suspicious-low flag) ────────────────────────
    if mileage is not None and year:
        from datetime import datetime as _dt
        age_years = max(1, _dt.now().year - year)
        km_per_year = mileage / age_years
        if age_years >= 3 and km_per_year < 4000 and mileage > 0:
            score -= 10  # likely clocked — big penalty
        elif mileage < 30000:
            score += 18
        elif mileage < 60000:
            score += 13
        elif mileage < 100000:
            score += 8
        elif mileage < 150000:
            score += 2
        elif mileage > 250000:
            score -= 15
        elif mileage > 200000:
            score -= 8
    elif mileage is not None:
        if mileage < 50000: score += 12
        elif mileage < 100000: score += 6
        elif mileage > 200000: score -= 10

    # ── History signals — the RARE ones that differentiate good listings ───
    # These are the main ranking signal when many cars have same year/mileage.
    if accident_free is True:
        score += 15
    elif accident_free is False:
        score -= 5
    if service_history is True:
        score += 10
    if previous_owners is not None:
        if previous_owners == 1:
            score += 8
        elif previous_owners == 2:
            score += 2
        elif previous_owners == 3:
            score -= 3
        elif previous_owners >= 4:
            score -= 10

    # ── Seller + warranty ──────────────────────────────────────────────────
    if seller_type and "dealer" in seller_type.lower():
        score += 2  # small — most listings are dealers already
    if warranty_months and warranty_months > 0:
        score += min(6, warranty_months // 4)

    # ── Features (max +12 — premium weighted heavier) ──────────────────────
    score += min(premium_feature_count * 2, 10)
    # Rest of features — diminishing returns
    extra_feat = max(0, feature_count - premium_feature_count)
    score += min(extra_feat // 3, 3)

    # ── Power (small bonus, not a major ranker) ────────────────────────────
    if hp:
        if hp >= 300: score += 3
        elif hp >= 200: score += 2
        elif hp >= 150: score += 1

    # ── Data completeness (max +5 — tiebreaker only) ───────────────────────
    if has_image: score += 1
    if gallery_count > 5: score += 2
    elif gallery_count > 0: score += 1
    if has_color and has_drive and has_body_type:
        score += 1  # listing is fully described

    return max(0, min(100, score))


# Premium feature keywords — these add most value to a used car.
# Multi-lingual: EN + DE + SV for cross-source matching.
PREMIUM_FEATURES = (
    # Panoramic roof
    "panorama", "panoramic", "panorama-roof", "panoramadach", "glasdach",
    "glastaket", "skytaket", "panoramataket", "panoramaglastaket",
    # Leather & premium upholstery
    "full leather", "leather", "leder", "leer", "skinn", "äkta läder",
    "skinnklädsel", "lederausstattung", "nappa", "dakota", "alcantara",
    "merino", "semi-aniline", "designo leather",
    # Seats — heated / ventilated / massage / memory
    "heated seats", "sitzheizung", "stolvärme", "stolvärmare",
    "ventilated seats", "kühlfunktion", "ventilerade", "aktiv belüftung",
    "ventilerad stol", "klimatiserade stolar",
    "massage", "massagefunktion", "massagesitze",
    "memory seats", "memorysätet", "sitzspeicher", "elminne",
    "electric seats", "el-säte", "elektrisch verstellbar", "elektrisk stol",
    "sportsitze", "sportstolar", "m sportsitze",
    # Digital cockpit / head-up display
    "head-up display", "hud", "head up", "head-up",
    "digital cockpit", "virtual cockpit", "live cockpit", "digitale tacho",
    "digitala instrument",
    # ADAS
    "adaptive cruise", "acc", "radar cruise", "adaptiv farthållare",
    "adaptiv", "aktiv acc", "stop & go",
    "lane assist", "spurassistent", "filhållare", "lane-keep",
    "blind spot", "toter winkel", "dödvinkel", "blindzon",
    "automatic parking", "parkassistent", "auto park", "parkstyrning",
    "traffic sign", "verkehrszeichen", "trafikskyltar",
    "night vision", "nachtsicht", "mörkerseende",
    # Headlights
    "matrix led", "laserlight", "laser light", "led matrix", "matrix headlight",
    "ipw", "xenon", "bi-xenon", "full led", "full-led", "adaptive led",
    "led strålkastare", "laserstrålkastare",
    # Keyless / comfort access
    "keyless", "keyless go", "keyless entry", "comfort access",
    "nyckellöst", "handsfree access",
    # 360 camera / surround view
    "360", "surround view", "birdview", "top view", "360 camera",
    "surround-view", "360-kamera", "360°", "rundumsicht",
    # Premium audio
    "harman", "harman kardon", "bang", "bang & olufsen", "burmester", "bose",
    "bowers", "bowers & wilkins", "meridian", "lexicon", "naim", "mark levinson",
    "premium sound", "surround sound",
    # Soft close doors, air suspension
    "soft close", "door servo", "softstängning",
    "air suspension", "luftfederung", "luftfjädring", "adaptive suspension",
    "adaptive dampers", "m adaptive",
    # Infotainment
    "apple carplay", "android auto", "wireless carplay",
    "wireless charging", "induktive ladestation", "induktiv laddning",
    "carplay", "android",
    # Towbar (Scandinavia big selling point)
    "tow hitch", "anhängerkupplung", "drag", "dragkrok", "elektrisch ausfahrbar",
    # Packages
    "m sport", "m-sport", "m paket", "amg line", "amg-line", "r-design",
    "s-line", "s line", "gt line", "st-line", "n-line", "r-linje",
    "first edition", "executive", "performance",
    # Misc premium
    "heated steering", "lenkradheizung", "ratvärme",
    "electric tailgate", "elektrische heckklappe", "el-baklucka",
    "sliding sunroof", "schiebedach", "soltak",
    "privacy glass", "privacy-scheiben",
)


def count_premium_features(*lists) -> int:
    """Count unique premium features across multiple feature lists."""
    seen = set()
    for lst in lists:
        if not lst:
            continue
        for f in lst:
            fl = (f or "").lower()
            for kw in PREMIUM_FEATURES:
                if kw in fl:
                    seen.add(kw)
                    break
    return len(seen)


