# parsers/parser_mobilede.py
# Mobile.de Search API — returns list[ParsedCar]

import re
import os
import base64
from typing import Optional

import aiohttp
from loguru import logger

from .base import (
    ParsedCar,
    normalize_make, normalize_model, normalize_fuel, normalize_transmission,
    normalize_color, normalize_body_type, normalize_drive,
    is_known_brand, calc_score, decode_html, FUEL_UA,
)

MOBILEDE_USER = os.getenv("MOBILEDE_USER")
MOBILEDE_PASS = os.getenv("MOBILEDE_PASS")

MOBILEDE_MAKES = {
    "bmw": "BMW", "audi": "AUDI", "mercedes-benz": "MERCEDES_BENZ",
    "volkswagen": "VW", "volvo": "VOLVO", "toyota": "TOYOTA",
    "honda": "HONDA", "mazda": "MAZDA", "skoda": "SKODA",
    "ford": "FORD", "opel": "OPEL", "peugeot": "PEUGEOT",
    "renault": "RENAULT", "hyundai": "HYUNDAI", "kia": "KIA",
    "nissan": "NISSAN", "porsche": "PORSCHE", "tesla": "TESLA",
    "mini": "MINI", "seat": "SEAT", "citroen": "CITROEN",
    "mitsubishi": "MITSUBISHI", "subaru": "SUBARU", "lexus": "LEXUS",
    "jeep": "JEEP", "jaguar": "JAGUAR", "land rover": "LAND_ROVER",
    "dacia": "DACIA", "fiat": "FIAT", "suzuki": "SUZUKI",
    "alfa romeo": "ALFA%20ROMEO", "genesis": "GENESIS", "cupra": "CUPRA",
}

# Map common model names to Mobile.de API model keys
# Discovered via open refdata API: services.mobile.de/refdata/classes/Car/makes/{MAKE}/models
MOBILEDE_MODELS = {
    # BMW
    ("BMW", "3er"): "320", ("BMW", "3 series"): "320", ("BMW", "3"): "320",
    ("BMW", "5er"): "520", ("BMW", "5 series"): "520", ("BMW", "5"): "520",
    ("BMW", "x3"): "X3", ("BMW", "x5"): "X5", ("BMW", "x1"): "X1",
    # Audi
    ("AUDI", "a4"): "A4", ("AUDI", "a6"): "A6", ("AUDI", "q5"): "Q5",
    ("AUDI", "a3"): "A3", ("AUDI", "q3"): "Q3",
    # Mercedes
    ("MERCEDES_BENZ", "c-class"): "C 200", ("MERCEDES_BENZ", "c-klasse"): "C 200",
    ("MERCEDES_BENZ", "e-class"): "E 200", ("MERCEDES_BENZ", "e-klasse"): "E 200",
    ("MERCEDES_BENZ", "glc"): "GLC 200",
    # VW
    ("VW", "golf"): "Golf", ("VW", "passat"): "Passat", ("VW", "tiguan"): "Tiguan",
    # Volvo
    ("VOLVO", "v60"): "V60", ("VOLVO", "xc60"): "XC60", ("VOLVO", "xc40"): "XC40",
    # Toyota
    ("TOYOTA", "rav4"): "RAV 4", ("TOYOTA", "corolla"): "Corolla",
    # Skoda
    ("SKODA", "octavia"): "Octavia", ("SKODA", "superb"): "Superb",
    # Porsche
    ("PORSCHE", "cayenne"): "Cayenne", ("PORSCHE", "macan"): "Macan",
    # Hyundai / Kia
    ("HYUNDAI", "tucson"): "Tucson", ("KIA", "sportage"): "Sportage",
}

BASE_URL = (
    "https://services.mobile.de/search-api/search"
    if os.getenv("MOBILEDE_PRODUCTION", "").lower() == "true"
    else "https://services.sandbox.mobile.de/search-api/search"
)

# Reverse of MOBILEDE_MAKES: API internal names → canonical display names
# e.g. "MERCEDES_BENZ" → "mercedes-benz",  "VW" → "volkswagen"
_MOBILEDE_MAKE_REVERSE: dict[str, str] = {v: k for k, v in MOBILEDE_MAKES.items()}


def _normalize_mobilede_make(raw: str) -> str:
    """Convert Mobile.de API make string to a name is_known_brand can check."""
    if not raw:
        return ""
    # Direct reverse lookup for internal format (MERCEDES_BENZ, VW, LAND_ROVER…)
    canonical = _MOBILEDE_MAKE_REVERSE.get(raw.upper())
    if canonical:
        return canonical
    # Also try with underscores replaced (LAND_ROVER → LAND ROVER fallback)
    return raw


def _parse_ad(ad: dict, min_year: int, min_price: float, max_mileage: int) -> Optional[ParsedCar]:
    ad_id = str(ad.get("mobileAdId", ""))
    url = ad.get("detailPageUrl") or f"https://www.mobile.de/auto-inserat/{ad_id}.html"

    raw_make = _normalize_mobilede_make(ad.get("make", ""))
    raw_model = ad.get("model", "") or ad.get("modelDescription", "")
    if not raw_make or not is_known_brand(raw_make):
        return None

    make = normalize_make(raw_make)
    model = normalize_model(raw_model, raw_make)

    # Year
    reg_str = ad.get("firstRegistration", "")
    year = int(reg_str[:4]) if reg_str and len(reg_str) >= 4 and reg_str[:4].isdigit() else None
    if year and year < min_year:
        return None

    # Price
    price_data = ad.get("price", {})
    price_eur = None
    try:
        raw = price_data.get("consumerPriceGross") or price_data.get("consumerPriceNet")
        price_eur = float(raw) if raw else None
    except (ValueError, TypeError):
        pass
    # Sanity check: prices > 500k EUR are likely in cents → divide by 100
    if price_eur is not None and price_eur > 500000:
        price_eur = price_eur / 100
    if price_eur is not None and price_eur < min_price:
        return None

    # Mileage
    mileage = ad.get("mileage")
    if mileage is not None and mileage > max_mileage:
        return None

    # Fuel
    fuel_raw = (ad.get("fuel") or "").upper()
    fuel = normalize_fuel(fuel_raw) or "Petrol"

    # Transmission
    gearbox = (ad.get("gearbox") or "").upper()
    transmission = "Automatic" if "AUTO" in gearbox else "Manual"

    # Power
    horsepower = None
    power_kw = ad.get("power")
    if power_kw:
        try:
            horsepower = int(float(power_kw) * 1.341)
        except (ValueError, TypeError):
            pass

    # Color
    color_raw = ad.get("exteriorColor") or ad.get("color") or ""
    color, color_ua = normalize_color(color_raw)

    # Body type
    body_raw = ad.get("category") or ad.get("bodyType") or ""
    body_type, body_type_ua = normalize_body_type(body_raw)

    # Drive — API field + inference from model description
    drive_raw = ad.get("drivetrain") or ad.get("driveType") or ""
    drive = normalize_drive(drive_raw)
    if not drive:
        model_desc_lower = (raw_model or "").lower()
        if any(k in model_desc_lower for k in ["quattro", "4matic", "xdrive", "4drive", "4x4", "4motion", "allrad", "awd"]):
            drive = "AWD"
        elif any(k in model_desc_lower for k in ["hinterrad", "rwd"]):
            drive = "RWD"
        elif any(k in model_desc_lower for k in ["frontantrieb", "fwd"]):
            drive = "FWD"

    # Engine — multiple strategies to extract displacement
    engine = None
    engine_cc = None  # exact displacement in cc, for numeric volume filtering

    # Strategy 1: cubicCapacity from API (in cc) → liters
    cubic = ad.get("cubicCapacity") or ad.get("displacement") or ad.get("engineSize")
    if cubic:
        try:
            cc = int(cubic)
            if cc > 100:  # sanity check — cc not liters
                engine_cc = cc
                liters = round(cc / 1000, 1)
                engine = f"{liters} {fuel}".strip() if fuel else str(liters)
        except (ValueError, TypeError):
            pass

    # Strategy 2: model description regex (e.g. "2.0 TDI", "1.5 TSI")
    if not engine:
        eng_m = re.search(r'\b([1-9]\.\d)\s*(?:TFSI|TDI|TSI|FSI|CDI|HDi|dCi|T)?\b', raw_model or "", re.IGNORECASE)
        if eng_m:
            engine = f"{eng_m.group(1)} {fuel}".strip() if fuel else eng_m.group(1)

    # Strategy 3: URL slug (e.g. "vw-passat-2-0-tdi-garding")
    if not engine:
        slug = url.lower()
        slug_m = re.search(r'\b(\d)-(\d+)-(tdi|tfsi|tsi|fsi|cdi|hdi|dci|petrol|diesel|benzin)\b', slug)
        if slug_m:
            engine = f"{slug_m.group(1)}.{slug_m.group(2)} {fuel}".strip()
        else:
            slug_m2 = re.search(r'-(\d)-(\d)-(?:tdi|tfsi|tsi|fsi|cdi|hdi|dci)', slug)
            if slug_m2:
                engine = f"{slug_m2.group(1)}.{slug_m2.group(2)} {fuel}".strip()

    # Strategy 4: power-based estimation (if kW known but no displacement)
    if not engine and power_kw:
        try:
            kw = float(power_kw)
            engine = f"{horsepower} к.с. {fuel}".strip() if horsepower else f"{int(kw)} kW {fuel}".strip()
        except (ValueError, TypeError):
            pass

    # Doors / seats — mobile.de returns string enums like "FOUR_OR_FIVE"
    _DOOR_MAP = {
        "TWO_OR_THREE": 3, "FOUR_OR_FIVE": 5, "SIX_OR_SEVEN": 7,
        "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5,
    }
    def _parse_count(raw) -> Optional[int]:
        if raw is None:
            return None
        if isinstance(raw, int):
            return raw
        s = str(raw).upper().strip()
        if s in _DOOR_MAP:
            return _DOOR_MAP[s]
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    # Use 'is not None' to avoid skipping 0 values (falsy but valid)
    _door_raw = ad.get("doorCount")
    if _door_raw is None: _door_raw = ad.get("numberOfDoors")
    if _door_raw is None: _door_raw = ad.get("doors")
    if _door_raw is None: _door_raw = ad.get("door_count")
    doors = _parse_count(_door_raw)

    _seat_raw = ad.get("numberOfSeats")
    if _seat_raw is None: _seat_raw = ad.get("seatCount")
    if _seat_raw is None: _seat_raw = ad.get("seats")
    if _seat_raw is None: _seat_raw = ad.get("seat_count")
    seats = _parse_count(_seat_raw)

    # Images
    image_list = ad.get("images", [])
    image = None
    gallery = []
    for img in image_list:
        if isinstance(img, dict):
            src = (
                img.get("xxxl") or img.get("xxl") or img.get("xl")
                or img.get("l") or img.get("m") or img.get("s") or ""
            )
        elif isinstance(img, str):
            src = img
        else:
            continue
        if src:
            if not image:
                image = src
            else:
                gallery.append(src)

    # Location
    seller = ad.get("seller", {})
    location = ""
    if seller:
        address = seller.get("address", {}) or {}
        location = address.get("city", "")

    # Features
    features_raw = ad.get("features", []) or ad.get("equipment", []) or []
    safety_features = []
    comfort_features = []
    infotainment = []
    features_other = []

    safety_kw = ["abs", "airbag", "esp", "lane", "blind", "emergency", "brake", "sensor", "camera"]
    info_kw = ["navigation", "carplay", "android", "bluetooth", "dab", "usb", "radio", "sound"]
    comfort_kw = ["heated", "leather", "panoram", "keyless", "climate", "cruise", "parking", "seat"]

    for feat in features_raw:
        if feat is None or isinstance(feat, (int, float, bool)):
            continue
        name = feat if isinstance(feat, str) else (feat.get("label") or feat.get("name") or feat.get("id") or "") if isinstance(feat, dict) else str(feat)
        name = decode_html(name).strip()
        if not name or len(name) < 2:
            continue
        # Humanize UPPER_SNAKE ids
        if name == name.upper() and "_" in name:
            name = name.replace("_", " ").title()

        nl = name.lower()
        if any(k in nl for k in safety_kw):
            safety_features.append(name)
        elif any(k in nl for k in info_kw):
            infotainment.append(name)
        elif any(k in nl for k in comfort_kw):
            comfort_features.append(name)
        else:
            features_other.append(name)

    car = ParsedCar(
        source_site="mobile.de",
        source_url=url,
        external_id=ad_id,
        make=make,
        model=model,
        year=year,
        price_eur=price_eur,
        mileage_km=mileage,
        fuel=fuel,
        fuel_ua=FUEL_UA.get(fuel, None),
        transmission=transmission,
        horsepower=horsepower,
        engine=engine,
        engine_cc=engine_cc,
        drive=drive,
        body_type=body_type,
        body_type_ua=body_type_ua,
        color=color,
        color_ua=color_ua,
        doors=doors,
        seats=seats,
        image=image,
        gallery=gallery,
        location=location,
        safety_features=safety_features,
        comfort_features=comfort_features,
        infotainment=infotainment,
        features_other=features_other,
        country="Germany",
    )

    car.score = calc_score(
        year=year, mileage=mileage, price_eur=price_eur,
        has_image=bool(image),
        has_features=bool(safety_features or comfort_features),
        hp=horsepower,
        gallery_count=len(gallery or []),
        feature_count=len(safety_features or []) + len(comfort_features or []) + len(infotainment or []),
        has_color=bool(color),
        has_drive=bool(drive),
        has_body_type=bool(body_type),
    )

    return car


import asyncio

_DETAIL_BASE = BASE_URL.rsplit("/search", 1)[0]  # "https://services[.sandbox].mobile.de/search-api"
_DETAIL_URL = f"{_DETAIL_BASE}/ad/{{ad_id}}"


async def _fetch_detail_features(
    session: aiohttp.ClientSession,
    headers: dict,
    car: ParsedCar,
    sem: asyncio.Semaphore,
) -> None:
    """Fetch individual ad detail page to get features/equipment."""
    if not car.external_id:
        return
    from .rate_limiter import acquire as _rl
    async with sem:
        await _rl("mobile.de")  # respect rate limits on detail fetches
        try:
            url = _DETAIL_URL.format(ad_id=car.external_id)
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return
                detail = await resp.json(content_type=None)

            ad = detail.get("ad", detail)

            # Engine: cubicCapacity from detail if not found in search
            if not car.engine or not car.engine_cc or (car.horsepower and car.engine.startswith(str(car.horsepower))):
                cubic = ad.get("cubicCapacity") or ad.get("displacement")
                if cubic:
                    try:
                        cc = int(cubic)
                        if cc > 100:
                            car.engine_cc = cc
                            liters = round(cc / 1000, 1)
                            car.engine = f"{liters} {car.fuel}".strip()
                    except (ValueError, TypeError):
                        pass

            # Description (free-text seller notes) — same detail call, no extra cost.
            if not car.description:
                desc = ad.get("description") or ad.get("freetext") or ad.get("text") or ""
                if isinstance(desc, str) and desc.strip():
                    car.description = decode_html(desc).strip()[:2000]

            # Features / equipment
            features_raw = (
                ad.get("features", [])
                or ad.get("equipment", [])
                or ad.get("equipments", [])
                or ad.get("specialEquipments", [])
                or []
            )
            if not features_raw:
                # Some ads nest features under "vehicle"
                vehicle = ad.get("vehicle", {})
                features_raw = (
                    vehicle.get("features", [])
                    or vehicle.get("equipment", [])
                    or []
                )

            safety_kw = ["abs", "airbag", "esp", "lane", "blind", "emergency", "brake", "sensor", "camera"]
            info_kw = ["navigation", "carplay", "android", "bluetooth", "dab", "usb", "radio", "sound"]
            comfort_kw = ["heated", "leather", "panoram", "keyless", "climate", "cruise", "parking", "seat"]

            for feat in features_raw:
                name = feat if isinstance(feat, str) else (
                    feat.get("label") or feat.get("name") or feat.get("id") or ""
                    if isinstance(feat, dict) else ""
                )
                name = decode_html(str(name)).strip()
                if not name or len(name) < 2:
                    continue
                if name == name.upper() and "_" in name:
                    name = name.replace("_", " ").title()

                nl = name.lower()
                if any(k in nl for k in safety_kw):
                    car.safety_features.append(name)
                elif any(k in nl for k in info_kw):
                    car.infotainment.append(name)
                elif any(k in nl for k in comfort_kw):
                    car.comfort_features.append(name)
                else:
                    car.features_other.append(name)

            if car.safety_features or car.comfort_features:
                logger.debug(f"[mobilede:detail] {car.make} {car.model}: "
                             f"{len(car.safety_features)}s/{len(car.comfort_features)}c/"
                             f"{len(car.infotainment)}i features")
        except Exception as e:
            logger.debug(f"[mobilede:detail] {car.external_id}: {e}")


async def fetch(
    brand: str = "",
    model_id: str = "",
    model_name: str = "",
    year_from: int = 2018,
    year_to: Optional[int] = None,
    price_max: Optional[int] = None,
    condition: str = "USED",
    sort_by: str = "creationTime",
    max_mileage: int = 200000,
    min_price: float = 3000,
    min_year: int = 2015,
    max_results: int = 50,
    vehicle_type: str = "Car",
) -> list[ParsedCar]:
    """Mobile.de Search API — no Playwright needed.

    If API credentials are missing, falls back to HTML scraping via mobilede_http.
    That path works only when MOBILEDE_PROXY_URL is set to a residential/consumer
    proxy (Akamai Bot Manager blocks datacenter IPs).
    """

    if not MOBILEDE_USER or not MOBILEDE_PASS:
        # Fallback: HTML scraping when no API creds.
        from . import mobilede_http
        html_results = await mobilede_http.search_html(
            brand=brand, model_name=model_name,
            year_from=year_from, year_to=year_to,
            price_max=price_max, max_mileage=max_mileage,
            max_results=max_results, vehicle_type=vehicle_type,
        )
        if not html_results:
            return []
        # Convert flat dicts → ParsedCar (reuse _parse_ad structure where possible).
        cars: list[ParsedCar] = []
        for h in html_results:
            try:
                make_raw = h.get("make", "")
                if not is_known_brand(make_raw):
                    continue
                make = normalize_make(make_raw)
                model = normalize_model(h.get("model", "") or "", make_raw)
                year = h.get("year")
                if year and year < min_year:
                    continue
                price = h.get("price")
                if price and price < min_price:
                    continue
                mileage = h.get("mileage")
                if mileage and mileage > max_mileage:
                    continue
                fuel = normalize_fuel(h.get("fuel") or "") or "Petrol"
                tx = h.get("transmission") or ""
                transmission = "Automatic" if "auto" in tx.lower() else "Manual" if "manuell" in tx.lower() or "manual" in tx.lower() else None
                images = h.get("images") or []
                car = ParsedCar(
                    source_site="mobile.de",
                    source_url=h.get("url", ""),
                    external_id=str(h.get("id", "")),
                    make=make, model=model, year=year,
                    price_eur=float(price) if price else None,
                    mileage_km=int(mileage) if mileage else None,
                    fuel=fuel, fuel_ua=FUEL_UA.get(fuel),
                    transmission=transmission,
                    horsepower=h.get("horsepower"),
                    image=images[0] if images else None,
                    gallery=images[1:] if len(images) > 1 else [],
                    country="Germany",
                )
                car.score = calc_score(
                    year=car.year, mileage=car.mileage_km, price_eur=car.price_eur,
                    has_image=bool(car.image), has_features=False, hp=car.horsepower,
                    gallery_count=len(car.gallery), feature_count=0,
                    has_color=False, has_drive=False, has_body_type=False,
                )
                cars.append(car)
            except Exception as e:
                logger.debug(f"[mobilede:html] parse failed: {e}")
        logger.info(f"[mobilede:html] {len(cars)} cars via HTML fallback")
        return cars

    credentials = base64.b64encode(f"{MOBILEDE_USER}:{MOBILEDE_PASS}".encode()).decode()
    headers = {
        "Accept": "application/vnd.de.mobile.api+json",
        "Authorization": f"Basic {credentials}",
        "Accept-Encoding": "gzip",
    }

    query: dict = {
        "condition": condition,
        "sort.field": sort_by,
        "sort.order": "DESCENDING",
        "page.size": min(max_results, 100),
        "page.number": 1,
        "damageUnrepaired": 0,
    }

    brand_key = brand.lower()
    mobilede_make = MOBILEDE_MAKES.get(brand_key)
    if mobilede_make:
        # Resolve model_id: explicit > lookup table > auto-convert
        effective_model_id = model_id
        if not effective_model_id and model_name:
            # Try lookup table first (covers common models)
            lookup_key = (mobilede_make, model_name.lower())
            effective_model_id = MOBILEDE_MODELS.get(lookup_key, "")
            if not effective_model_id:
                # Auto-convert: "3er" → "3ER", "A-Class" → "A%20CLASS"
                # Mobile.de uses URL-encoded spaces, not underscores
                effective_model_id = re.sub(r'[\s-]+', '%20', model_name.upper()).strip('%20')
                effective_model_id = re.sub(r'[^A-Z0-9%]', '', effective_model_id)

        if effective_model_id:
            query["classification"] = f"refdata/classes/Car/makes/{mobilede_make}/models/{effective_model_id}"
        else:
            query["classification"] = f"refdata/classes/Car/makes/{mobilede_make}"
        logger.info(f"[mobilede] make={mobilede_make} model={effective_model_id or '(all)'}")
    else:
        query["classification"] = "refdata/classes/Car"
        if brand:
            logger.warning(f"[mobilede] Unknown make: {brand}")

    query["firstRegistrationDate.min"] = f"{year_from}-01"
    if year_to:
        query["firstRegistrationDate.max"] = f"{year_to}-12"
    if price_max:
        query["price.max"] = price_max
    query["mileage.max"] = max_mileage

    logger.info(f"[mobilede] API: brand={brand} model_id={model_id} year_from={year_from}")

    deals: list[ParsedCar] = []
    try:
        from .rate_limiter import acquire as _rl
        await _rl("mobile.de")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                BASE_URL, headers=headers, params=query,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"[mobilede] HTTP {resp.status}: {text[:300]}")
                    return []
                data = await resp.json(content_type=None)

            ads = data.get("ads", [])
            logger.info(f"[mobilede] Found: {data.get('total', 0)}, got: {len(ads)}")

            for ad in ads:
                try:
                    car = _parse_ad(ad, min_year, min_price, max_mileage)
                    if car:
                        deals.append(car)
                except Exception as e:
                    logger.debug(f"[mobilede:ad] {e}")

            # Fetch detail pages for features & engine (parallel, max 5 concurrent)
            if deals:
                sem = asyncio.Semaphore(5)
                detail_tasks = [
                    _fetch_detail_features(session, headers, car, sem)
                    for car in deals
                ]
                await asyncio.gather(*detail_tasks, return_exceptions=True)
                logger.info(f"[mobilede] Detail fetched for {len(deals)} cars")

        # Limit: max 2 cars from same dealer for same model
        seen_seller: dict = {}
        filtered = []
        for car in deals:
            key = f"{car.make}_{car.model}_{car.location}"
            count = seen_seller.get(key, 0)
            if count < 2:
                seen_seller[key] = count + 1
                filtered.append(car)
        deals = filtered

        logger.info(f"[mobilede] Parsed: {len(deals)}")
    except Exception as e:
        logger.error(f"[mobilede] {e}")

    return deals