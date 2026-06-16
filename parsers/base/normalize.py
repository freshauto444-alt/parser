# parsers/base/normalize.py — make/model/fuel/colour/body/drive/transmission
# normalization + inference. Extracted from base.py.

import re
from typing import Optional

from .constants import (
    KNOWN_BRANDS, MAKE_NORMALIZE, FUEL_NORMALIZE, FUEL_UA,
    COLOR_EN, COLOR_UA, BODY_EN, BODY_UA, DRIVE_NORMALIZE, TRANS_NORMALIZE,
)

# ══════════════════════════════════════════════════════════════════════════════
#  NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════

def normalize_make(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, dict):
        raw = raw.get("name") or raw.get("label") or ""
    raw = str(raw)
    if not raw.strip():
        return ""
    return MAKE_NORMALIZE.get(raw.strip().upper(), raw.strip().title())


def normalize_model(raw: str, make: str = "") -> str:
    if not raw or not raw.strip():
        return ""
    m = raw.strip()
    # Remove make prefix from model
    if make:
        make_words = make.lower().split()
        model_words = m.lower().split()
        while model_words and model_words[0] in make_words:
            model_words.pop(0)
        if model_words:
            orig = m.split()
            m = " ".join(orig[len(orig) - len(model_words):])
    # Series/class conversion FIRST
    m = re.sub(r'^(\d)er$', r'\1 Series', m, flags=re.IGNORECASE)
    m = re.sub(r'^([a-z])-?class$', lambda x: f"{x.group(1).upper()}-Class", m, flags=re.IGNORECASE)
    m = re.sub(r'^([a-z])-?klasse$', lambda x: f"{x.group(1).upper()}-Class", m, flags=re.IGNORECASE)
    # Short alphanumeric codes WITH digits: 320, A4, X5, RS6
    if len(m) <= 5 and re.match(r'^[A-Za-z0-9]+$', m) and re.search(r'\d', m):
        return m.upper()
    return m.strip()


def is_known_brand(text: str) -> bool:
    return text.lower().strip() in KNOWN_BRANDS


def normalize_fuel(raw) -> Optional[str]:
    if not raw:
        return None
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("name") or raw.get("formatted") or ""
    raw = str(raw)
    key = raw.strip().lower()
    if key in FUEL_NORMALIZE:
        return FUEL_NORMALIZE[key]
    for p, v in FUEL_NORMALIZE.items():
        if p in key:
            return v
    return None


def parse_fuel_from_text(text: str) -> Optional[str]:
    tl = text.lower()
    if any(k in tl for k in ["elbil", "elektro", "electric", "elektrisk"]):
        return "Electric"
    if "hybrid" in tl or "laddhybrid" in tl:
        return "Hybrid"
    if "diesel" in tl:
        return "Diesel"
    if any(k in tl for k in ["bensin", "petrol", "benzin", "gasoline"]):
        return "Petrol"
    return None


def normalize_color(raw: str) -> tuple[Optional[str], Optional[str]]:
    if not raw:
        return None, None
    key = raw.strip().lower()
    en = COLOR_EN.get(key)
    if en:
        return en, COLOR_UA.get(en)
    for p, ev in COLOR_EN.items():
        if p in key:
            return ev, COLOR_UA.get(ev)
    return raw.strip().title(), None


def normalize_body_type(raw: str) -> tuple[Optional[str], Optional[str]]:
    if not raw:
        return None, None
    key = raw.strip().lower()
    en = BODY_EN.get(key)
    if en:
        return en, BODY_UA.get(en)

    # Brand-specific Estate-variant keywords — must be checked BEFORE the
    # generic substring loop, otherwise "Avant" would match "van" inside
    # BODY_EN and become misclassified as a Van. These are all common AS24
    # variant strings for the Estate body of European cars:
    #   Volkswagen Passat Variant, Golf Variant
    #   Audi A4/A6 Avant, A6 Allroad
    #   BMW 3/5 Series Touring
    #   Mercedes E-Class T-Modell, CLA Shooting Brake
    #   Peugeot 308/508 SW (Sports Wagon)
    #   Opel Astra Sports Tourer, Insignia Country Tourer
    #   Skoda Octavia/Superb Combi, Octavia Scout
    #   Volvo V60/V90 Cross Country
    #   VW Passat Alltrack
    #   Toyota Corolla Touring Sports / Sportstourer
    #   Cupra Leon Sportstourer
    ESTATE_VARIANT_TOKENS = (
        "variant", "avant", "touring", "t-modell", "tmodell", "sportstourer",
        "sports tourer", "shooting brake", "country tourer", "cross country",
        "alltrack", "allroad", "scout", " sw", "combi", "kombi", "wagon",
    )
    for tok in ESTATE_VARIANT_TOKENS:
        if tok in key:
            return "Estate", BODY_UA.get("Estate")

    for p, ev in BODY_EN.items():
        if p in key:
            return ev, BODY_UA.get(ev)
    # Unknown value (often trim level like "AMG Line", "M Sport") — leave NULL
    # so the strict filter trusts source-side URL filter instead of dropping.
    return None, None


# ── Model → body type inference ─────────────────────────────────────────────
# Hardcoded mapping for popular models. Used when sources don't extract body
# from variant/category fields. Covers ~80% of common European car listings.
MODEL_BODY: dict[str, str] = {
    # ── Sedans ──
    "passat": "Sedan", "jetta": "Sedan", "vento": "Sedan",
    "a3 sedan": "Sedan", "a4": "Sedan", "a6": "Sedan", "a8": "Sedan", "a3 limousine": "Sedan",
    "1 series sedan": "Sedan", "2 series gran coupe": "Sedan", "3 series": "Sedan",
    "3er": "Sedan", "5 series": "Sedan", "5er": "Sedan", "7 series": "Sedan", "7er": "Sedan",
    "c-class": "Sedan", "c-klasse": "Sedan", "e-class": "Sedan", "e-klasse": "Sedan",
    "s-class": "Sedan", "s-klasse": "Sedan", "cls": "Sedan",
    "s60": "Sedan", "s90": "Sedan",
    "model 3": "Sedan", "model s": "Sedan",
    "ioniq 6": "Sedan", "i4": "Sedan", "i5": "Sedan", "i7": "Sedan",
    "octavia": "Sedan",  # Octavia is liftback/sedan-like
    "superb": "Sedan", "rapid": "Sedan",
    "insignia": "Sedan", "astra sedan": "Sedan",
    "mondeo": "Sedan", "focus sedan": "Sedan",
    "camry": "Sedan", "corolla sedan": "Sedan", "avensis": "Sedan",
    "accord": "Sedan", "civic sedan": "Sedan",
    "elantra": "Sedan", "sonata": "Sedan",
    "rio sedan": "Sedan", "k5": "Sedan", "stinger": "Sedan",
    "mazda6": "Sedan", "mazda3 sedan": "Sedan",
    "panamera": "Sedan",
    "giulia": "Sedan",
    "xe": "Sedan", "xf": "Sedan", "xj": "Sedan",
    "9-3": "Sedan", "9-5": "Sedan",
    "ds 7": "SUV",  # DS 7 is SUV
    "408": "Sedan", "508": "Sedan",
    "et5": "Sedan", "et7": "Sedan",
    # ── SUVs ──
    "x1": "SUV", "x2": "SUV", "x3": "SUV", "x4": "SUV", "x5": "SUV", "x6": "SUV", "x7": "SUV",
    "ix": "SUV", "ix1": "SUV", "ix3": "SUV",
    "q2": "SUV", "q3": "SUV", "q4": "SUV", "q5": "SUV", "q7": "SUV", "q8": "SUV",
    "q4 e-tron": "SUV", "q8 e-tron": "SUV", "e-tron": "SUV",
    "gla": "SUV", "glb": "SUV", "glc": "SUV", "gle": "SUV", "gls": "SUV",
    "g-class": "SUV", "g-klasse": "SUV", "eqa": "SUV", "eqb": "SUV", "eqc": "SUV", "eqe suv": "SUV", "eqs suv": "SUV",
    "tiguan": "SUV", "touareg": "SUV", "t-cross": "SUV", "t-roc": "SUV", "taos": "SUV", "atlas": "SUV",
    "id.4": "SUV", "id.5": "SUV", "id.6": "SUV", "id.7": "SUV",
    "rav4": "SUV", "highlander": "SUV", "c-hr": "SUV", "land cruiser": "SUV", "yaris cross": "SUV", "corolla cross": "SUV", "bz4x": "SUV",
    "cr-v": "SUV", "hr-v": "SUV", "pilot": "SUV", "passport": "SUV",
    "tucson": "SUV", "santa fe": "SUV", "kona": "SUV", "ix35": "SUV", "ix55": "SUV", "venue": "SUV", "palisade": "SUV", "nexo": "SUV",
    "sportage": "SUV", "sorento": "SUV", "stonic": "SUV", "soul": "SUV", "niro": "SUV", "ev6": "SUV", "ev9": "SUV", "telluride": "SUV", "seltos": "SUV",
    "xc40": "SUV", "xc60": "SUV", "xc70": "SUV", "xc90": "SUV", "ex30": "SUV", "ex90": "SUV",
    "evoque": "SUV", "discovery": "SUV", "discovery sport": "SUV", "defender": "SUV", "freelander": "SUV", "velar": "SUV",
    "range rover": "SUV", "range rover sport": "SUV", "range rover evoque": "SUV", "range rover velar": "SUV",
    "cayenne": "SUV", "macan": "SUV",
    "stelvio": "SUV", "tonale": "SUV",
    "f-pace": "SUV", "e-pace": "SUV", "i-pace": "SUV",
    "yeti": "SUV", "kodiaq": "SUV", "karoq": "SUV", "enyaq": "SUV",
    "kuga": "SUV", "edge": "SUV", "puma": "SUV", "explorer": "SUV", "escape": "SUV", "ecosport": "SUV", "bronco": "SUV", "mustang mach-e": "SUV",
    "mokka": "SUV", "crossland": "SUV", "grandland": "SUV", "frontera": "SUV", "antara": "SUV",
    "qashqai": "SUV", "x-trail": "SUV", "juke": "SUV", "murano": "SUV", "pathfinder": "SUV", "patrol": "SUV", "ariya": "SUV",
    "captur": "SUV", "kadjar": "SUV", "koleos": "SUV", "arkana": "SUV", "austral": "SUV", "rafale": "SUV", "scenic e-tech": "SUV",
    "duster": "SUV", "bigster": "SUV",
    "3008": "SUV", "5008": "SUV", "2008": "SUV", "4008": "SUV", "rifter": "SUV", "e-2008": "SUV", "e-3008": "SUV",
    "c3 aircross": "SUV", "c4 aircross": "SUV", "c5 aircross": "SUV", "c5 x": "SUV", "berlingo": "SUV", "spacetourer": "SUV",
    "rexton": "SUV", "korando": "SUV", "tivoli": "SUV", "musso": "SUV", "torres": "SUV",
    "outlander": "SUV", "asx": "SUV", "eclipse cross": "SUV",
    "cx-3": "SUV", "cx-30": "SUV", "cx-5": "SUV", "cx-60": "SUV", "cx-9": "SUV", "mx-30": "SUV",
    "forester": "SUV", "outback": "SUV", "ascent": "SUV", "solterra": "SUV", "crosstrek": "SUV",
    "wrangler": "SUV", "cherokee": "SUV", "grand cherokee": "SUV", "compass": "SUV", "renegade": "SUV", "avenger": "SUV", "commander": "SUV",
    "model x": "SUV", "model y": "SUV",
    "atto 3": "SUV", "yuan plus": "SUV", "song plus": "SUV", "sealion": "SUV",
    "mg zs": "SUV", "mg hs": "SUV", "mg marvel": "SUV", "mg one": "SUV",
    "ora 03": "Hatchback", "ora 07": "Sedan", "ora cat": "Hatchback",
    "haval h6": "SUV", "haval jolion": "SUV", "wey 03": "SUV",
    "ds 3": "SUV", "ds 4": "SUV", "ds 5": "SUV", "ds 7 crossback": "SUV", "ds 9": "Sedan",
    "formentor": "SUV", "ateca": "SUV", "tarraco": "SUV", "born": "Hatchback", "tavascan": "SUV",
    "leon": "Hatchback",
    # ── Hatchbacks ──
    "golf": "Hatchback", "polo": "Hatchback", "up!": "Hatchback", "lupo": "Hatchback",
    "id.3": "Hatchback", "id.2": "Hatchback",
    "fiesta": "Hatchback", "ka": "Hatchback",
    "focus": "Hatchback",
    "yaris": "Hatchback", "aygo": "Hatchback", "iq": "Hatchback",
    "corolla": "Hatchback", "auris": "Hatchback",
    "civic": "Hatchback", "jazz": "Hatchback",
    "i10": "Hatchback", "i20": "Hatchback", "i30": "Hatchback", "ioniq": "Hatchback", "ioniq 5": "Hatchback",
    "rio": "Hatchback", "ceed": "Hatchback", "picanto": "Hatchback",
    "208": "Hatchback", "308": "Hatchback", "108": "Hatchback", "e-208": "Hatchback", "e-308": "Hatchback",
    "c1": "Hatchback", "c2": "Hatchback", "c3": "Hatchback", "c4": "Hatchback", "ds 3 crossback": "Hatchback",
    "scala": "Hatchback", "fabia": "Hatchback", "citigo": "Hatchback",
    "ibiza": "Hatchback", "mii": "Hatchback",
    "mg3": "Hatchback", "mg4": "Hatchback", "mg5": "Hatchback",
    "twingo": "Hatchback", "clio": "Hatchback", "zoe": "Hatchback", "modus": "Hatchback",
    "sandero": "Hatchback", "lodgy": "Hatchback", "logan": "Hatchback",
    "500": "Hatchback", "500e": "Hatchback", "panda": "Hatchback", "punto": "Hatchback", "tipo": "Hatchback",
    "corsa": "Hatchback", "adam": "Hatchback", "karl": "Hatchback", "agila": "Hatchback",
    "astra": "Hatchback",
    "swift": "Hatchback", "celerio": "Hatchback", "ignis": "Hatchback", "baleno": "Hatchback",
    "micra": "Hatchback", "leaf": "Hatchback", "note": "Hatchback", "pulsar": "Hatchback",
    "mazda2": "Hatchback", "mazda3": "Hatchback",
    "1 series": "Hatchback", "1er": "Hatchback", "i3": "Hatchback",
    "a-class": "Hatchback", "a-klasse": "Hatchback", "b-class": "Hatchback", "b-klasse": "Hatchback",
    "a1": "Hatchback", "a2": "Hatchback",
    "mini": "Hatchback", "cooper": "Hatchback", "one": "Hatchback",
    "smart fortwo": "Hatchback", "fortwo": "Hatchback", "forfour": "Hatchback",
    # ── Estates / wagons ──
    "v60": "Estate", "v70": "Estate", "v90": "Estate", "v40": "Estate", "v50": "Estate",
    "passat variant": "Estate", "passat alltrack": "Estate", "golf variant": "Estate", "golf alltrack": "Estate",
    "a4 avant": "Estate", "a6 avant": "Estate", "a4 allroad": "Estate", "a6 allroad": "Estate",
    "rs4 avant": "Estate", "rs6 avant": "Estate",
    "3 series touring": "Estate", "5 series touring": "Estate",
    "c-class t-modell": "Estate", "e-class t-modell": "Estate", "cla shooting brake": "Estate",
    "octavia combi": "Estate", "superb combi": "Estate", "fabia combi": "Estate",
    "308 sw": "Estate", "508 sw": "Estate", "308 estate": "Estate",
    "focus estate": "Estate", "mondeo estate": "Estate",
    "astra sports tourer": "Estate", "insignia sports tourer": "Estate",
    "leon st": "Estate", "ibiza st": "Estate",
    # ── Coupes ──
    "tt": "Coupe", "tts": "Coupe", "ttrs": "Coupe", "rs5": "Coupe", "r8": "Coupe",
    "4 series": "Coupe", "4er": "Coupe", "8 series": "Coupe", "8er": "Coupe",
    "z4": "Convertible",
    "amg gt": "Coupe", "sl": "Convertible", "slk": "Convertible", "slc": "Convertible",
    "718 cayman": "Coupe", "911": "Coupe", "718 boxster": "Convertible", "boxster": "Convertible", "cayman": "Coupe",
    "supra": "Coupe", "gr supra": "Coupe", "86": "Coupe", "gr86": "Coupe", "gr yaris": "Hatchback",
    "rcz": "Coupe",
    # ── Convertibles ──
    "miata": "Convertible", "mx-5": "Convertible",
    "beetle convertible": "Convertible", "maggiolino": "Convertible",
    "cooper cabrio": "Convertible", "cooper d cabrio": "Convertible", "cooper sd cabrio": "Convertible",
    "eos": "Convertible",
    "mustang convertible": "Convertible",
    # ── Vans / minivans ──
    "caddy": "Van", "transporter": "Van", "multivan": "Van", "california": "Van",
    "vito": "Van", "v-class": "Van", "v-klasse": "Van", "marco polo": "Van", "sprinter": "Van", "viano": "Van",
    "berlingo cargo": "Van", "spacetourer cargo": "Van",
    "partner": "Van", "boxer": "Van", "expert": "Van", "traveller": "Van",
    "kangoo": "Van", "trafic": "Van", "master": "Van",
    "vivaro": "Van", "movano": "Van", "combo": "Van", "zafira": "Van", "zafira tourer": "Van",
    "doblo": "Van", "scudo": "Van", "ducato": "Van", "talento": "Van", "qubo": "Van",
    "transit": "Van", "tourneo": "Van", "tourneo connect": "Van", "tourneo custom": "Van",
    "nv200": "Van", "nv400": "Van", "primastar": "Van", "interstar": "Van",
    "h350": "Van", "h-1": "Van", "staria": "Van", "starex": "Van",
    # ── Pickups ──
    "ranger": "Pickup", "raptor": "Pickup", "f-150": "Pickup", "f-250": "Pickup",
    "amarok": "Pickup", "hilux": "Pickup", "navara": "Pickup", "frontier": "Pickup",
    "l200": "Pickup", "triton": "Pickup", "d-max": "Pickup", "rodeo": "Pickup",
    "1500": "Pickup", "ram": "Pickup", "silverado": "Pickup",
    "x-class": "Pickup", "x-klasse": "Pickup",
}


def _infer_make_specific(make: str, model: str) -> Optional[str]:
    """Brand-specific body inference for numeric/code-style models.
    AS24 often returns BMW="118", Mercedes="C 200", Audi="A4" etc. instead of
    full model names. These need brand-aware decoding.
    """
    import re as _re
    mk = make.strip().lower()
    md = model.strip().lower()
    if not mk or not md:
        return None

    if mk == "bmw":
        # Letter prefix
        if md.startswith("x"):
            return "SUV"
        # Series digit at start (118, 320, 540, M3, M5, etc.)
        m = _re.match(r"^[mi]?(\d)", md)
        if m:
            d = m.group(1)
            # 1, 2 series mostly Hatchback/Compact; 3, 5, 7 mostly Sedan; 4, 6, 8 mostly Coupe
            # Override if "touring"/"gran coupe"/"cabrio" in model name
            if "touring" in md or "kombi" in md:
                return "Estate"
            if "cabrio" in md or "convertible" in md:
                return "Convertible"
            if "gran coupe" in md or "gran turismo" in md or "coupe" in md:
                return "Coupe"
            if d in ("1", "2"):
                return "Hatchback"
            if d in ("3", "5", "7"):
                return "Sedan"
            if d in ("4", "6", "8"):
                return "Coupe"

    if mk == "mercedes-benz" or mk == "mercedes":
        # Cabrio/Convertible markers
        if "cabrio" in md or "convertible" in md or md.startswith("sl"):
            return "Convertible"
        # SUV families: GLA, GLB, GLC, GLE, GLS, G, EQA, EQB, EQC
        if _re.match(r"^(gla|glb|glc|gle|gls|g|eqa|eqb|eqc|eqe suv|eqs suv|g-)", md):
            return "SUV"
        # Coupe families: CLA, CLS, CLK, AMG GT
        if _re.match(r"^(cla|cls|clk|amg gt)", md):
            return "Coupe"
        # T-Modell / Estate
        if "t-modell" in md or "kombi" in md or " t " in md:
            return "Estate"
        # Vans: V-Class, Vito, Marco Polo, Sprinter, Citan
        if _re.match(r"^(v[\s-]|vito|marco|sprinter|citan|viano)", md):
            return "Van"
        # Sedans (default for letter+number): A 200, B 180, C 220, E 350, S 500
        if _re.match(r"^[abcdes][\s-]?\d", md):
            # A-class is hatchback, B-class is mini-MPV/hatchback
            if md.startswith("a") or md.startswith("b"):
                return "Hatchback"
            return "Sedan"
        # Pickup: X-Class
        if md.startswith("x"):
            return "Pickup"

    if mk == "audi":
        # SUVs: Q*, e-tron
        if md.startswith("q") or "e-tron" in md or "etron" in md:
            return "SUV"
        # RS variants: usually wagon/sedan based on number
        if md.startswith("rs"):
            if "avant" in md or "kombi" in md:
                return "Estate"
            if "coupe" in md or md in ("rs5", "rs7", "rsq3"):
                return "Coupe"
        # A-models
        if "avant" in md or "allroad" in md or "kombi" in md:
            return "Estate"
        if "cabrio" in md or "convertible" in md:
            return "Convertible"
        if "sportback" in md:
            return "Hatchback"
        if md == "a1" or md.startswith("a1 "):
            return "Hatchback"
        if md.startswith("a") and len(md) >= 2 and md[1].isdigit():
            return "Sedan"
        # TT, R8 are coupe/convertible
        if md.startswith("tt") or md.startswith("r8"):
            return "Coupe"

    if mk == "porsche":
        if md.startswith("cayenne") or md.startswith("macan"):
            return "SUV"
        if md.startswith("panamera"):
            return "Sedan"
        if "cabrio" in md or "convertible" in md or "spyder" in md:
            return "Convertible"
        return "Coupe"  # 911, 718, Cayman, Boxster

    if mk == "fiat":
        # 500 family
        if md.startswith("500x"):
            return "SUV"
        if md.startswith("500l"):
            return "Van"
        if md.startswith("500"):
            return "Hatchback"
        if md.startswith("ducato") or md.startswith("doblo") or md.startswith("scudo"):
            return "Van"

    if mk in ("ds", "ds automobiles"):
        # DS 3, 4, 5 = Hatchback; DS 7 = SUV; DS 9 = Sedan
        m = _re.match(r"^(?:ds[\s]?)?(\d)", md)
        if m:
            d = m.group(1)
            if d in ("3", "4"):
                return "Hatchback"
            if d == "5":
                return "Estate"  # DS 5 was sedan/wagon hybrid
            if d == "7":
                return "SUV"
            if d == "9":
                return "Sedan"

    if mk == "tesla":
        if md.startswith("model x") or md.startswith("model y"):
            return "SUV"
        return "Sedan"  # Model 3, S

    if mk == "mazda":
        if md.startswith("cx") or md.startswith("mx-30"):
            return "SUV"
        if md.startswith("mx-5") or md.startswith("miata"):
            return "Convertible"
        # Bare numbers from AS24: "2", "3", "6"
        if md == "2" or md.startswith("2 ") or md == "mazda2":
            return "Hatchback"
        if md == "3" or md.startswith("3 ") or md == "mazda3":
            return "Sedan"  # mostly Sedan in EU; some Hatch variants
        if md == "6" or md.startswith("6 ") or md == "mazda6":
            return "Sedan"

    if mk == "citroen":
        # Electric variants (E-C4, E-Berlingo, etc.)
        if md.startswith("e-c4") or md.startswith("ec4"):
            return "Hatchback"
        if md.startswith("c1") or md.startswith("c2") or md.startswith("c3"):
            return "Hatchback"
        if md.startswith("c4 cactus"):
            return "SUV"
        if md.startswith("c4 aircross") or md.startswith("c5 aircross") or md.startswith("c3 aircross"):
            return "SUV"
        if md.startswith("c5 x"):
            return "SUV"
        if md.startswith("c5"):
            return "Sedan"
        if md.startswith("c4"):
            return "Hatchback"
        if md.startswith("c6"):
            return "Sedan"
        if md.startswith("berlingo") or md.startswith("spacetourer") or md.startswith("jumpy") or md.startswith("jumper"):
            return "Van"

    if mk == "peugeot":
        # Numeric models — odd hundreds typically Hatchback (208, 308),
        # 4-digit (3008, 5008) are SUVs, 4xx/5xx are Sedans
        m = _re.match(r"^e?-?(\d+)", md)
        if m:
            num_str = m.group(1)
            num = int(num_str)
            if num >= 1000:  # 3008, 5008, 2008, 4008
                return "SUV"
            # SW variants
            if "sw" in md:
                return "Estate"
            if num in (107, 108, 207, 208, 307, 308):
                return "Hatchback"
            if num in (407, 408, 508, 607, 608):
                return "Sedan"
            if num == 505:
                return "Sedan"
        if md.startswith("rifter") or md.startswith("partner") or md.startswith("expert") or md.startswith("traveller") or md.startswith("boxer") or md.startswith("tepee"):
            return "Van"

    if mk == "renault":
        if md.startswith("scenic") and "e-tech" in md:
            return "SUV"
        if md.startswith("captur") or md.startswith("kadjar") or md.startswith("koleos") or md.startswith("arkana") or md.startswith("austral") or md.startswith("rafale"):
            return "SUV"
        if md.startswith("clio") or md.startswith("twingo") or md.startswith("zoe") or md.startswith("modus"):
            return "Hatchback"
        if md.startswith("megane"):
            if "e-tech" in md or "e tech" in md:
                return "SUV"
            return "Hatchback"
        if md.startswith("kangoo") or md.startswith("trafic") or md.startswith("master") or md.startswith("express"):
            return "Van"

    if mk == "kia":
        if md.startswith("ev") or md.startswith("niro") or md.startswith("sportage") or md.startswith("sorento") or md.startswith("stonic") or md.startswith("soul") or md.startswith("seltos") or md.startswith("telluride") or md.startswith("mohave"):
            return "SUV"
        if md.startswith("picanto") or md.startswith("rio") or md.startswith("ceed") or md.startswith("venga") or md.startswith("stonic"):
            return "Hatchback"
        if md.startswith("k") and len(md) >= 2 and md[1].isdigit():
            return "Sedan"  # K3, K4, K5, K7, K9
        if md.startswith("optima") or md.startswith("cadenza") or md.startswith("stinger") or md.startswith("forte"):
            return "Sedan"
        if md.startswith("carnival") or md.startswith("carens"):
            return "Van"

    if mk == "hyundai":
        if md.startswith("ix") or md.startswith("kona") or md.startswith("tucson") or md.startswith("santa") or md.startswith("palisade") or md.startswith("nexo") or md.startswith("venue") or md.startswith("creta"):
            return "SUV"
        if md.startswith("ioniq 5") or md.startswith("ioniq5"):
            return "Hatchback"
        if md.startswith("ioniq 6") or md.startswith("ioniq6"):
            return "Sedan"
        if md.startswith("ioniq"):
            return "Hatchback"
        if md.startswith("i") and len(md) >= 2 and md[1].isdigit():
            return "Hatchback"  # i10, i20, i30, i40
        if md.startswith("elantra") or md.startswith("sonata") or md.startswith("accent") or md.startswith("azera"):
            return "Sedan"
        if md.startswith("h-1") or md.startswith("h1") or md.startswith("h-350") or md.startswith("staria"):
            return "Van"

    if mk == "ford":
        if md.startswith("kuga") or md.startswith("edge") or md.startswith("explorer") or md.startswith("escape") or md.startswith("ecosport") or md.startswith("bronco") or md.startswith("puma") or md.startswith("territory") or md.startswith("mustang mach"):
            return "SUV"
        if md.startswith("fiesta") or md.startswith("focus") or md.startswith("ka"):
            return "Hatchback"
        if md.startswith("mondeo"):
            return "Sedan"
        if md.startswith("mustang") and "mach" not in md:
            return "Coupe"
        if md.startswith("transit") or md.startswith("tourneo"):
            return "Van"
        if md.startswith("ranger") or md.startswith("f-150") or md.startswith("f-250") or md.startswith("raptor"):
            return "Pickup"

    if mk == "opel" or mk == "vauxhall":
        if md.startswith("mokka") or md.startswith("crossland") or md.startswith("grandland") or md.startswith("frontera") or md.startswith("antara"):
            return "SUV"
        if md.startswith("corsa") or md.startswith("adam") or md.startswith("astra") or md.startswith("karl") or md.startswith("agila"):
            return "Hatchback"
        if md.startswith("insignia"):
            return "Sedan"
        if md.startswith("zafira") or md.startswith("vivaro") or md.startswith("movano") or md.startswith("combo") or md.startswith("meriva"):
            return "Van"

    if mk == "volvo":
        if md.startswith("xc") or md.startswith("ex"):
            return "SUV"
        if md.startswith("v") and len(md) >= 2 and md[1].isdigit():
            return "Estate"  # V40, V50, V60, V70, V90
        if md.startswith("s") and len(md) >= 2 and md[1].isdigit():
            return "Sedan"  # S40, S60, S80, S90
        if md.startswith("c") and len(md) >= 2 and md[1].isdigit():
            return "Coupe"  # C30, C70

    if mk == "skoda":
        if md.startswith("kodiaq") or md.startswith("karoq") or md.startswith("yeti") or md.startswith("enyaq") or md.startswith("kamiq"):
            return "SUV"
        if "combi" in md or "scout" in md:
            return "Estate"
        if md.startswith("octavia"):
            return "Sedan"
        if md.startswith("superb"):
            return "Sedan"
        if md.startswith("scala") or md.startswith("fabia") or md.startswith("rapid") or md.startswith("citigo"):
            return "Hatchback"

    if mk in ("seat", "cupra"):
        if md.startswith("ateca") or md.startswith("tarraco") or md.startswith("formentor") or md.startswith("tavascan") or md.startswith("arona"):
            return "SUV"
        if md.startswith("born"):
            return "Hatchback"
        if md.startswith("ibiza") or md.startswith("mii") or md.startswith("leon") or md.startswith("arosa"):
            return "Hatchback"
        if md.startswith("toledo") or md.startswith("exeo"):
            return "Sedan"
        if md.startswith("alhambra") or md.startswith("altea"):
            return "Van"

    if mk == "volkswagen" or mk == "vw":
        if md.startswith("tiguan") or md.startswith("touareg") or md.startswith("t-cross") or md.startswith("t-roc") or md.startswith("taos") or md.startswith("atlas") or md.startswith("teramont"):
            return "SUV"
        if md.startswith("id.4") or md.startswith("id.5") or md.startswith("id.6") or md.startswith("id.7"):
            return "SUV"
        if md.startswith("id.3") or md.startswith("id.2"):
            return "Hatchback"
        if "variant" in md or "alltrack" in md:
            return "Estate"
        if md.startswith("passat") or md.startswith("jetta") or md.startswith("vento") or md.startswith("phaeton") or md.startswith("arteon"):
            return "Sedan"
        if md.startswith("golf") or md.startswith("polo") or md.startswith("up") or md.startswith("lupo") or md.startswith("fox"):
            return "Hatchback"
        if md.startswith("beetle") or md.startswith("maggiolino"):
            if "convertible" in md or "cabrio" in md:
                return "Convertible"
            return "Hatchback"
        if md.startswith("eos"):
            return "Convertible"
        if md.startswith("transporter") or md.startswith("multivan") or md.startswith("caddy") or md.startswith("california") or md.startswith("crafter") or md.startswith("amarok"):
            if md.startswith("amarok"):
                return "Pickup"
            return "Van"

    if mk == "toyota":
        if md.startswith("rav") or md.startswith("highlander") or md.startswith("c-hr") or md.startswith("chr") or md.startswith("land cruiser") or md.startswith("4runner") or md.startswith("fortuner") or md.startswith("yaris cross") or md.startswith("corolla cross") or md.startswith("bz4x"):
            return "SUV"
        if md.startswith("yaris") and "cross" not in md:
            return "Hatchback"
        if md.startswith("corolla"):
            if "verso" in md or "wagon" in md or "estate" in md or "kombi" in md or "touring" in md:
                return "Estate"
            if "sedan" in md:
                return "Sedan"
            return "Sedan"
        if md.startswith("camry") or md.startswith("avensis") or md.startswith("auris") or md.startswith("prius") or md.startswith("mirai"):
            return "Sedan"
        if md.startswith("aygo") or md.startswith("iq"):
            return "Hatchback"
        if md.startswith("verso") or md.startswith("hiace") or md.startswith("proace"):
            return "Van"
        if md.startswith("hilux"):
            return "Pickup"
        if md.startswith("supra") or md.startswith("gr86") or md.startswith("celica"):
            return "Coupe"

    if mk == "nissan":
        if md.startswith("qashqai") or md.startswith("x-trail") or md.startswith("juke") or md.startswith("murano") or md.startswith("pathfinder") or md.startswith("patrol") or md.startswith("ariya") or md.startswith("kicks"):
            return "SUV"
        if md.startswith("micra") or md.startswith("note") or md.startswith("pulsar") or md.startswith("leaf"):
            return "Hatchback"
        if md.startswith("almera") or md.startswith("primera") or md.startswith("teana") or md.startswith("maxima") or md.startswith("sentra"):
            return "Sedan"
        if md.startswith("nv") or md.startswith("primastar") or md.startswith("interstar"):
            return "Van"
        if md.startswith("navara") or md.startswith("frontier") or md.startswith("titan"):
            return "Pickup"
        if md.startswith("370z") or md.startswith("350z") or md.startswith("gt-r") or md.startswith("gtr"):
            return "Coupe"

    return None


def infer_body_from_model(make: str, model: str) -> tuple[Optional[str], Optional[str]]:
    """Infer body_type from make+model when source data lacks it.
    Resolution order:
      1. Make-specific rules (BMW 118 → Hatchback, Mercedes GLC → SUV)
      2. Direct exact match in MODEL_BODY
      3. Prefix match (longest first)
      4. Substring match
    """
    if not model:
        return None, None
    m = model.strip().lower()
    if not m:
        return None, None

    # Try make-specific rules first (most accurate for code-style models)
    en = _infer_make_specific(make, model)
    if en:
        return en, BODY_UA.get(en)

    # Direct exact match
    en = MODEL_BODY.get(m)
    if en:
        return en, BODY_UA.get(en)

    # Prefix match — longest first
    candidates = sorted(
        ((k, v) for k, v in MODEL_BODY.items() if m.startswith(k + " ") or m == k),
        key=lambda x: -len(x[0]),
    )
    if candidates:
        en = candidates[0][1]
        return en, BODY_UA.get(en)

    # Substring match (only for 4+ char keys to avoid false positives)
    for k, v in MODEL_BODY.items():
        if len(k) >= 4 and k in m:
            return v, BODY_UA.get(v)

    return None, None


def normalize_drive(raw: str) -> Optional[str]:
    if not raw:
        return None
    key = raw.strip().lower()
    if key in DRIVE_NORMALIZE:
        return DRIVE_NORMALIZE[key]
    for p, v in DRIVE_NORMALIZE.items():
        if p in key:
            return v
    return None


def normalize_transmission(raw: str) -> Optional[str]:
    if not raw:
        return None
    key = raw.strip().lower()
    if key in TRANS_NORMALIZE:
        return TRANS_NORMALIZE[key]
    for p, v in TRANS_NORMALIZE.items():
        if p in key:
            return v
    return None

