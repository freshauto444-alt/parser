# parsers/base.py
# Unified data model + normalization + translation for all parsers.

import re
import html
import uuid as uuid_lib
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timedelta, timezone
from loguru import logger

# ══════════════════════════════════════════════════════════════════════════════
#  BRAND NORMALIZATION
# ══════════════════════════════════════════════════════════════════════════════

KNOWN_BRANDS = {
    "audi", "bmw", "mercedes", "mercedes-benz", "volkswagen", "vw",
    "volvo", "toyota", "honda", "mazda", "skoda", "seat", "cupra",
    "ford", "opel", "peugeot", "renault", "citroen", "hyundai", "kia",
    "nissan", "mitsubishi", "subaru", "lexus", "porsche", "tesla",
    "mini", "jeep", "land rover", "landrover", "jaguar", "alfa romeo",
    "alfa", "saab", "suzuki", "dacia", "fiat", "chrysler", "dodge",
    "chevrolet", "cadillac", "genesis",
}

MAKE_NORMALIZE = {
    "BMW": "BMW", "VW": "Volkswagen", "VOLKSWAGEN": "Volkswagen",
    "MERCEDES": "Mercedes-Benz", "MERCEDES-BENZ": "Mercedes-Benz",
    "LAND ROVER": "Land Rover", "LANDROVER": "Land Rover",
    "ALFA": "Alfa Romeo", "ALFA ROMEO": "Alfa Romeo",
    "MINI": "MINI", "SAAB": "Saab", "KIA": "Kia",
    "AUDI": "Audi", "VOLVO": "Volvo", "TOYOTA": "Toyota",
    "HONDA": "Honda", "MAZDA": "Mazda", "SKODA": "Skoda",
    "SEAT": "SEAT", "CUPRA": "Cupra",
    "FORD": "Ford", "OPEL": "Opel", "PEUGEOT": "Peugeot",
    "RENAULT": "Renault", "CITROEN": "Citroen",
    "HYUNDAI": "Hyundai", "NISSAN": "Nissan",
    "MITSUBISHI": "Mitsubishi", "SUBARU": "Subaru",
    "LEXUS": "Lexus", "PORSCHE": "Porsche", "TESLA": "Tesla",
    "JEEP": "Jeep", "JAGUAR": "Jaguar",
    "SUZUKI": "Suzuki", "DACIA": "Dacia", "FIAT": "Fiat",
    "CHRYSLER": "Chrysler", "DODGE": "Dodge",
    "CHEVROLET": "Chevrolet", "CADILLAC": "Cadillac",
    "GENESIS": "Genesis",
}

FUEL_NORMALIZE = {
    "petrol": "Petrol", "gasoline": "Petrol", "benzin": "Petrol",
    "bensin": "Petrol", "diesel": "Diesel",
    "electric": "Electric", "elektro": "Electric", "elektrisk": "Electric",
    "elbil": "Electric", "el": "Electric",
    "hybrid": "Hybrid", "laddhybrid": "Hybrid",
    "hybrid_petrol": "Hybrid", "hybrid_diesel": "Hybrid",
    "lpg": "LPG", "cng": "CNG",
}

FUEL_UA = {
    "Petrol": "Бензин", "Diesel": "Дизель",
    "Electric": "Електро", "Hybrid": "Гібрид",
    "LPG": "Газ", "CNG": "Газ",
}

COLOR_EN = {
    "black": "Black", "white": "White", "silver": "Silver",
    "grey": "Grey", "gray": "Grey", "blue": "Blue",
    "red": "Red", "green": "Green", "brown": "Brown",
    "beige": "Beige", "orange": "Orange", "yellow": "Yellow",
    "gold": "Gold", "purple": "Purple", "bronze": "Bronze",
    # Swedish (with special chars)
    "svart": "Black", "vit": "White",
    "grå": "Grey", "gra": "Grey",
    "blå": "Blue", "bla": "Blue",
    "röd": "Red", "rod": "Red",
    "grön": "Green", "gron": "Green",
    "brun": "Brown", "gul": "Yellow", "guld": "Gold", "lila": "Purple",
    # German
    "schwarz": "Black", "weiss": "White", "weiß": "White",
    "silber": "Silver", "grau": "Grey", "blau": "Blue",
    "rot": "Red", "grün": "Green", "braun": "Brown", "gelb": "Yellow",
}

COLOR_UA = {
    "Black": "Чорний", "White": "Білий", "Silver": "Сріблястий",
    "Grey": "Сірий", "Blue": "Синій", "Red": "Червоний",
    "Green": "Зелений", "Brown": "Коричневий", "Beige": "Бежевий",
    "Orange": "Помаранчевий", "Yellow": "Жовтий", "Gold": "Золотий",
    "Purple": "Фіолетовий", "Bronze": "Бронзовий",
}

BODY_EN = {
    "sedan": "Sedan", "saloon": "Sedan", "limousine": "Sedan",
    "estate": "Estate", "station wagon": "Estate", "wagon": "Estate",
    "suv": "SUV", "crossover": "SUV", "offroad": "SUV",
    "hatchback": "Hatchback",
    "coupe": "Coupe", "convertible": "Convertible", "cabriolet": "Convertible",
    "van": "Van", "minivan": "Van", "mpv": "Van",
    "pickup": "Pickup",
    "kombi": "Estate", "halvkombi": "Hatchback",
    "cab": "Convertible", "minibuss": "Van",
}

BODY_UA = {
    "Sedan": "Седан", "Estate": "Універсал", "SUV": "Позашляховик",
    "Hatchback": "Хетчбек", "Coupe": "Купе", "Convertible": "Кабріолет",
    "Van": "Мінівен", "Pickup": "Пікап",
}

DRIVE_NORMALIZE = {
    "front wheel drive": "FWD", "fwd": "FWD", "framhjulsdrift": "FWD",
    "front_wheel": "FWD", "frontwheel": "FWD",
    "rear wheel drive": "RWD", "rwd": "RWD", "bakhjulsdrift": "RWD",
    "rear_wheel": "RWD", "rearwheel": "RWD",
    "all wheel drive": "AWD", "awd": "AWD", "4wd": "AWD",
    "all_wheel": "AWD", "allwheel": "AWD",
    "fyrhjulsdrift": "AWD", "4-motion": "AWD", "quattro": "AWD", "xdrive": "AWD",
}

TRANS_NORMALIZE = {
    "automatic": "Automatic", "auto": "Automatic", "automatik": "Automatic",
    "automat": "Automatic", "dsg": "Automatic", "tiptronic": "Automatic",
    "s tronic": "Automatic", "pdk": "Automatic", "cvt": "Automatic",
    "manual": "Manual", "manuell": "Manual", "schaltgetriebe": "Manual",
}


# ══════════════════════════════════════════════════════════════════════════════
#  SWEDISH FEATURE TRANSLATION
# ══════════════════════════════════════════════════════════════════════════════

SV_FEATURES: dict[str, tuple[str, str]] = {
    # Safety
    "abs-bromsar": ("ABS", "safety"),
    "airbag förare": ("Driver airbag", "safety"),
    "airbag passagerare fram": ("Passenger airbag", "safety"),
    "avstängningsbar airbag passagerare": ("Deactivatable passenger airbag", "safety"),
    "sidoairbags": ("Side airbags", "safety"),
    "sidokrockgardiner": ("Side curtain airbags", "safety"),
    "antisladd": ("Electronic stability control", "safety"),
    "autobroms": ("Automatic emergency braking", "safety"),
    "broms-assistans": ("Brake assist", "safety"),
    "backstartshjälp": ("Hill start assist", "safety"),
    "körfilsassistans": ("Lane keeping assist", "safety"),
    "laneassist": ("Lane assist", "safety"),
    "trötthetsvarnare": ("Driver drowsiness detection", "safety"),
    "startspärr": ("Immobilizer", "safety"),
    "stöldlarm": ("Alarm system", "safety"),
    "barnlås": ("Child lock", "safety"),
    "isofix-fästen bak": ("ISOFIX rear", "safety"),
    "isofix-fästen fram": ("ISOFIX front", "safety"),
    "ljussensor": ("Light sensor", "safety"),
    "regnsensor": ("Rain sensor", "safety"),
    "parkeringssensorer": ("Parking sensors", "safety"),
    "backkamera": ("Rear camera", "safety"),
    "dimljus fram": ("Front fog lights", "safety"),
    "led strålkastare": ("LED headlights", "safety"),
    "led (halvljus)": ("LED low beam", "safety"),
    "bi-xenonstrålkastare": ("Bi-xenon headlights", "safety"),
    "xenon (helljus)": ("Xenon high beam", "safety"),
    "fotgängardetektion": ("Pedestrian detection", "safety"),
    "nödsamtal": ("Emergency call system", "safety"),
    "euro ncap 5": ("Euro NCAP 5 stars", "safety"),
    "euro 6": ("Euro 6 emissions", "safety"),
    "fartbegränsare": ("Speed limiter", "safety"),
    # Comfort
    "farthållare": ("Cruise control", "comfort"),
    "farthållare (adaptiv)": ("Adaptive cruise control", "comfort"),
    "adaptiv farthållare": ("Adaptive cruise control", "comfort"),
    "acc": ("Adaptive cruise control", "comfort"),
    "sätesvärme (fram)": ("Front seat heating", "comfort"),
    "rattvärme": ("Heated steering wheel", "comfort"),
    "delbart baksäte": ("Split rear seat", "comfort"),
    "fällbara baksäten": ("Folding rear seats", "comfort"),
    "elhissar (fram och bak)": ("Electric windows front+rear", "comfort"),
    "elhissar (fram)": ("Electric windows front", "comfort"),
    "elinfällbara sidospeglar": ("Electrically folding mirrors", "comfort"),
    "eluppvärmda sidospeglar": ("Heated side mirrors", "comfort"),
    "centrallås (fjärrstyrt)": ("Remote central locking", "comfort"),
    "keyless nyckelfri start": ("Keyless entry + start", "comfort"),
    "multifunktionsratt": ("Multi-function steering wheel", "comfort"),
    "sportratt": ("Sport steering wheel", "comfort"),
    "sportstolar": ("Sport seats", "comfort"),
    "luftkonditionering": ("Air conditioning", "comfort"),
    "acc 2 klimatzoner": ("Dual-zone climate control", "comfort"),
    "dragkrok": ("Tow bar", "comfort"),
    "dragkrok (utfällbar)": ("Retractable tow bar", "comfort"),
    "elbaklucka": ("Electric tailgate", "comfort"),
    "tonade rutor": ("Tinted windows", "comfort"),
    "rails": ("Roof rails", "comfort"),
    "servostyrning": ("Power steering", "comfort"),
    "start-/stoppfunktion": ("Start-stop system", "comfort"),
    "motorvärmare (fjärrstyrd)": ("Remote engine heater", "comfort"),
    "motorvärmare (med tidur)": ("Engine heater with timer", "comfort"),
    "bränslevärmare": ("Fuel heater", "comfort"),
    "uppvärmda spolare": ("Heated washer nozzles", "comfort"),
    "kylt handskfack": ("Cooled glove compartment", "comfort"),
    "plant lastutrymme": ("Flat cargo area", "comfort"),
    "avbländande innerbackspegel": ("Auto-dimming rear mirror", "comfort"),
    "läslampa": ("Reading light", "comfort"),
    "sminkspegel": ("Vanity mirror", "comfort"),
    # Infotainment
    "bluetooth (handsfree)": ("Bluetooth handsfree", "infotainment"),
    "android auto": ("Android Auto", "infotainment"),
    "apple carplay": ("Apple CarPlay", "infotainment"),
    "usb-uttag": ("USB port", "infotainment"),
    "aux-ingång": ("AUX input", "infotainment"),
    "cd-stereo": ("CD player", "infotainment"),
    "digitalradio (dab)": ("Digital radio DAB", "infotainment"),
    "gps": ("Navigation system", "infotainment"),
    "färddator": ("On-board computer", "infotainment"),
    "touch-/pekskärm": ("Touch screen", "infotainment"),
    "digitalt mätarhus": ("Digital cockpit", "infotainment"),
    "akustikrutor": ("Acoustic glass", "infotainment"),
    "trådlös telefonladdare": ("Wireless phone charging", "infotainment"),
    # Comfort (additional)
    "panoramatak": ("Panoramic roof", "comfort"),
    "panoramatakvindor": ("Panoramic sunroof", "comfort"),
    "panoramaglastak": ("Panoramic glass roof", "comfort"),
    "soltak": ("Sunroof", "comfort"),
    "taklucka": ("Sunroof", "comfort"),
    "motorvärmare": ("Engine heater", "comfort"),
    "webasto": ("Webasto heater", "comfort"),
    "kupévärmare": ("Cabin heater", "comfort"),
    "el-stolar": ("Electric seats", "comfort"),
    "elsäte förare": ("Electric driver seat", "comfort"),
    "elsäte passagerare": ("Electric passenger seat", "comfort"),
    "minnesfunktion säte": ("Seat memory function", "comfort"),
    "skinnklädsel": ("Leather upholstery", "comfort"),
    "skinninredning": ("Leather interior", "comfort"),
    "alcantara": ("Alcantara upholstery", "comfort"),
    "ryggmassage": ("Seat massage", "comfort"),
    "armstöd": ("Armrest", "comfort"),
    "lastgaller": ("Cargo net", "comfort"),
    "lastnät": ("Cargo net", "comfort"),
    "parkeringshjälp": ("Parking assist", "comfort"),
    "automatisk parkering": ("Automatic parking", "comfort"),
    "luftfjädring": ("Air suspension", "comfort"),
    "adaptiv fjädring": ("Adaptive suspension", "comfort"),
    "head-up display": ("Head-up display", "comfort"),
    "head up display": ("Head-up display", "comfort"),
    "ambitionsbelysning": ("Ambient lighting", "comfort"),
    "ambient belysning": ("Ambient lighting", "comfort"),
    "induktionsladdning": ("Wireless charging", "comfort"),
    "eluppvärmd vindruta": ("Heated windshield", "comfort"),
    "uppvärmd vindruta": ("Heated windshield", "comfort"),
    # Safety (additional)
    "360-kamera": ("360-degree camera", "safety"),
    "360 graders kamera": ("360-degree camera", "safety"),
    "dödvinkelassistans": ("Blind spot assist", "safety"),
    "dödvinkelvarning": ("Blind spot warning", "safety"),
    "avståndsvarnare": ("Distance warning", "safety"),
    "skyltavläsare": ("Speed sign recognition", "safety"),
    "trafikskyltavläsning": ("Traffic sign recognition", "safety"),
    "nattseende": ("Night vision", "safety"),
    "adaptiva strålkastare": ("Adaptive headlights", "safety"),
    "adaptiv kurvljus": ("Adaptive cornering lights", "safety"),
    "helljusassistans": ("High beam assist", "safety"),
    "dragkontroll": ("Traction control", "safety"),
    "dimmljus bak": ("Rear fog lights", "safety"),
    "matrixljus": ("Matrix LED headlights", "safety"),
    "laserljus": ("Laser headlights", "safety"),
    # Infotainment (additional)
    "soundsystem": ("Premium sound system", "infotainment"),
    "harman kardon": ("Harman Kardon sound", "infotainment"),
    "bose": ("Bose sound system", "infotainment"),
    "bang & olufsen": ("Bang & Olufsen sound", "infotainment"),
    "b&o": ("Bang & Olufsen sound", "infotainment"),
    "burmester": ("Burmester sound system", "infotainment"),
    "wifi": ("WiFi hotspot", "infotainment"),
    "wlan": ("WiFi hotspot", "infotainment"),
    "navigationssystem": ("Navigation system", "infotainment"),
    "fjärrstrålkastare": ("High beam headlights", "safety"),
    "digital tv": ("Digital TV", "infotainment"),
    # Other
    "svensksåld": ("Sold new in Sweden", "other"),
    "yttertemperaturmätare": ("Outside temperature display", "other"),
    "7-sits": ("7 seats", "other"),
    "5-sits": ("5 seats", "other"),
    "s-line": ("S-Line package", "other"),
    "svart optik": ("Black optics package", "other"),
    "m-sport": ("M-Sport package", "other"),
    "amg": ("AMG package", "other"),
    "r-design": ("R-Design package", "other"),
    "inscription": ("Inscription package", "other"),
    "momentum": ("Momentum package", "other"),
    "sportline": ("Sportline package", "other"),
    "vinterhjul": ("Winter wheels", "other"),
    "sommarhjul": ("Summer wheels", "other"),
    "reservhjul": ("Spare wheel", "other"),
    "lättmetallfälgar": ("Alloy wheels", "other"),
    "aluminiumfälgar": ("Aluminum wheels", "other"),
    "autohold": ("Auto hold", "comfort"),
    "fjärrstyrd motorvärmare": ("Remote engine preheater", "comfort"),
}


# ══════════════════════════════════════════════════════════════════════════════
#  UNIFIED DATA MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ParsedCar:
    source_site: str
    source_url: str
    external_id: str
    make: str = ""
    model: str = ""
    year: Optional[int] = None
    price_eur: Optional[float] = None
    mileage_km: Optional[int] = None
    fuel: Optional[str] = None
    fuel_ua: Optional[str] = None
    transmission: Optional[str] = None
    horsepower: Optional[int] = None
    drive: Optional[str] = None
    body_type: Optional[str] = None
    body_type_ua: Optional[str] = None
    engine: Optional[str] = None
    color: Optional[str] = None
    color_ua: Optional[str] = None
    doors: Optional[int] = None
    seats: Optional[int] = None
    seat_material: Optional[str] = None
    seat_material_ua: Optional[str] = None
    image: Optional[str] = None
    gallery: list[str] = field(default_factory=list)
    safety_features: list[str] = field(default_factory=list)
    comfort_features: list[str] = field(default_factory=list)
    infotainment: list[str] = field(default_factory=list)
    features_other: list[str] = field(default_factory=list)
    location: Optional[str] = None
    country: str = "Europe"
    condition: str = "Used"
    score: int = 50
    price_original: Optional[float] = None
    price_currency: Optional[str] = None
    # Quality signals — the more of these we have, the better we can rank.
    previous_owners: Optional[int] = None     # 1 owner is premium, 3+ is red flag
    accident_free: Optional[bool] = None      # Unfallfrei / olycksfri / no damage
    service_history: Optional[bool] = None    # Scheckheftgepflegt / full service book
    has_damage: Optional[bool] = None         # explicit damage listed (for filter-out)
    warranty_months: Optional[int] = None     # remaining factory / dealer warranty
    seller_type: Optional[str] = None         # "dealer" / "private" — dealers average better
    inspection_valid_until: Optional[str] = None  # TÜV/besiktning YYYY-MM
    # Multi-vehicle support — "Car" (default), "Motorcycle", "Truck", "Van",
    # "Bus", "Camper", "Trailer", "ConstructionMachine", "Tractor".
    vehicle_type: str = "Car"
    engine_cc: Optional[int] = None  # cubic centimetres — main metric for motorcycles
    vin: Optional[str] = None  # when exposed — unlocks cross-source dedup

    # ── Extended spec / environment ──────────────────────────────────────────
    interior_color: Optional[str] = None          # e.g. "Black leather", "Beige Dakota"
    interior_color_ua: Optional[str] = None
    emission_class: Optional[str] = None          # "Euro 6", "Euro 6d-TEMP"
    co2_emissions_g_km: Optional[int] = None      # g/km CO2 — WLTP
    fuel_consumption_combined: Optional[float] = None  # L/100km or kWh/100km
    energy_efficiency: Optional[str] = None       # "A+", "A", "B" …
    cylinders: Optional[int] = None
    gears: Optional[int] = None                   # 6, 7, 8, 9, 10
    weight_kg: Optional[int] = None               # empty weight
    tow_capacity_kg: Optional[int] = None

    # ── Sales / listing ──────────────────────────────────────────────────────
    description: Optional[str] = None             # seller's free-text description
    title_line: Optional[str] = None              # original listing title
    first_registration: Optional[str] = None      # "MM/YYYY" for display

    # ── Dealer info ──────────────────────────────────────────────────────────
    dealer_name: Optional[str] = None
    dealer_rating: Optional[float] = None         # stars 0..5
    dealer_review_count: Optional[int] = None
    dealer_phone: Optional[str] = None
    dealer_website: Optional[str] = None
    dealer_city: Optional[str] = None
    dealer_zip: Optional[str] = None

    # ── Video (AS24 has it in vehicle.video.src) ────────────────────────────
    video_url: Optional[str] = None

    # Timestamp of listing update (for "fresh" scoring)
    listing_updated_at: Optional[str] = None


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


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def decode_html(text: str) -> str:
    if not text:
        return ""
    return html.unescape(html.unescape(text)).strip()


def clean_int(text: str) -> Optional[int]:
    if not text:
        return None
    nums = re.sub(r"[^\d]", "", text)
    return int(nums) if nums else None


def extract_uuid(url: str) -> Optional[str]:
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", url, re.I)
    return m.group(1) if m else None


def sek_to_eur(sek: float, rate: float = 0.088) -> float:
    return round(sek * rate)


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


# ══════════════════════════════════════════════════════════════════════════════
#  DB ROW BUILDER
# ══════════════════════════════════════════════════════════════════════════════

COUNTRY_UA = {
    "Europe": "Європа",
    "Germany": "Німеччина",
    "Sweden": "Швеція",
    "France": "Франція",
    "Italy": "Італія",
    "Spain": "Іспанія",
    "Netherlands": "Нідерланди",
    "Belgium": "Бельгія",
    "Austria": "Австрія",
    "Switzerland": "Швейцарія",
    "Poland": "Польща",
    "Czech Republic": "Чехія",
    "Denmark": "Данія",
    "Norway": "Норвегія",
    "Finland": "Фінляндія",
    "UK": "Великобританія",
    "Ireland": "Ірландія",
    "Portugal": "Португалія",
    "Luxembourg": "Люксембург",
    "Hungary": "Угорщина",
    "Romania": "Румунія",
    "Croatia": "Хорватія",
    "Slovenia": "Словенія",
    "Slovakia": "Словаччина",
    "Bulgaria": "Болгарія",
    "Lithuania": "Литва",
    "Latvia": "Латвія",
    "Estonia": "Естонія",
}


def build_db_row(
    car: ParsedCar,
    source_type: str,
    expires_at: str,
    client_order_id: Optional[str] = None,
    template_id: Optional[str] = None,
) -> dict:
    return {
        "id": str(uuid_lib.uuid4()),
        "source_type": source_type,
        "source_url": car.source_url,
        "source_site": car.source_site,
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
        "make": car.make,
        "model": car.model,
        "year": car.year,
        "price": car.price_eur,
        "mileage": car.mileage_km,
        "fuel": car.fuel or "Petrol",
        "fuel_ua": car.fuel_ua or FUEL_UA.get(car.fuel or "Petrol", "Бензин"),
        "transmission": car.transmission or "Automatic",
        "horsepower": car.horsepower,
        "engine": car.engine,
        "drive": car.drive or "Unknown",
        "body_type": car.body_type or "Unknown",
        "body_type_ua": car.body_type_ua or BODY_UA.get(car.body_type or "", "Невідомо"),
        "color": car.color or "Unknown",
        "color_ua": car.color_ua or "Невідомо",
        "doors": car.doors,
        "seats": car.seats,
        "seat_material": car.seat_material,
        "seat_material_ua": car.seat_material_ua,
        "image": car.image or "",
        "gallery": deduplicate_gallery(car.gallery or []),
        "features": [],
        "features_ua": car.features_other,
        "safety_features": car.safety_features,
        "comfort_features": car.comfort_features,
        "infotainment": car.infotainment,
        "status": "Available",
        "status_ua": "Доступно",
        "verified": False,
        "condition": car.condition,
        "condition_ua": "Вживаний" if car.condition == "Used" else "Новий",
        "country": car.country,
        "country_ua": COUNTRY_UA.get(car.country, car.country),
        "plate_type": "EU",
        "vin": None,
        "history": [],
        "client_order_id": client_order_id,
    }