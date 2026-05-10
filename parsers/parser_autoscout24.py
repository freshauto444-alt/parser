# parsers/parser_autoscout24.py
# AutoScout24 parser — returns list[ParsedCar].
#
# Strategy:
#   1. List page via Playwright (JS renders cards)
#   2. Extract card URLs + basic info (price, year, mileage from card text)
#   3. Detail pages via HTTP + __NEXT_DATA__ JSON (no browser needed)
#      → make, model, color, drive, body, doors, seats, equipment, photos
#   4. Make/model come from __NEXT_DATA__ (100% reliable), NOT from card regex

import asyncio
import re
import json
from typing import Optional

import httpx
from loguru import logger
from playwright.async_api import BrowserContext

from .base import (
    ParsedCar,
    normalize_make, normalize_model, normalize_color, normalize_body_type,
    normalize_drive, normalize_fuel, normalize_transmission,
    parse_fuel_from_text, extract_uuid, decode_html, calc_score,
    is_known_brand, DRIVE_NORMALIZE, FUEL_UA,
)

BASE_URL = "https://www.autoscout24.com"

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9,de;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}

# Prefer curl_cffi for detail fetches — AS24's Akamai Bot Manager reads JA3/JA4
# fingerprints, and httpx uses OpenSSL (obviously non-browser signature).
try:
    from curl_cffi.requests import AsyncSession as _CurlSession  # type: ignore
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

# Import URL building from as24_http (canonical location — avoids circular imports)
from .as24_http import (
    build_as24_url as build_url,
    AS24_FUEL as _AS24_FUEL,
    AS24_TRANS as _AS24_TRANS,
    AS24_BODY as _AS24_BODY,
    AS24_MODEL_SLUG as _AS24_MODEL_SLUG,
)


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP-ONLY PARSER — uses _next/data JSON API (no Playwright)
# ══════════════════════════════════════════════════════════════════════════════

async def parse_http(params: dict, max_results: int = 20) -> list[ParsedCar]:
    """
    HTTP-only AS24 parser — extracts __NEXT_DATA__ from HTML.
    No Playwright needed. 1 HTTP request = 20 cars with full data.
    Enriches top 5 results with detail page data (color, drive, body, features).
    """
    from .as24_http import search as as24_search

    listings, total = await as24_search(params, max_results=max_results)

    if not listings:
        logger.info("[as24:http] No listings returned")
        return []

    cars: list[ParsedCar] = []
    for raw in listings:
        try:
            d = raw

            make = normalize_make(d.get("make", ""))
            model_raw = d.get("model", "") or d.get("model_group", "")
            model = normalize_model(model_raw, make)

            # Fuel: normalize + fallback to text parsing
            fuel_raw = d.get("fuel") or ""
            fuel = normalize_fuel(fuel_raw) or parse_fuel_from_text(fuel_raw) or None
            fuel_ua = FUEL_UA.get(fuel or "", None)

            transmission = normalize_transmission(d.get("transmission")) if d.get("transmission") else None

            # Body type — try multiple sources in order of reliability:
            # 1. variant field (often "Cabriolet"/"Touring"/"Avant")
            # 2. model_group/subtitle (sometimes have body keywords)
            # 3. MODEL_BODY hardcoded inference from make+model
            # AS24's &body=N URL filter is unreliable (~30% leakage), so explicit
            # detection from data fields is critical.
            from .base import infer_body_from_model
            body_type, body_type_ua = None, None
            for src_field in ("variant", "model", "model_group", "subtitle"):
                src_val = d.get(src_field) or ""
                if not src_val:
                    continue
                bt, bt_ua = normalize_body_type(src_val)
                if bt:
                    body_type, body_type_ua = bt, bt_ua
                    break
            if not body_type:
                body_type, body_type_ua = infer_body_from_model(make, model)

            ext_id = d.get("id") or extract_uuid(d.get("url", "")) or ""
            images = d.get("images", [])

            car = ParsedCar(
                source_site="autoscout24.com",
                source_url=d.get("url", ""),
                external_id=ext_id,
                make=make, model=model,
                year=d.get("year"),
                price_eur=d.get("price"),
                mileage_km=d.get("mileage"),
                fuel=fuel, fuel_ua=fuel_ua,
                transmission=transmission,
                horsepower=d.get("horsepower"),
                engine=d.get("engine"),
                body_type=body_type, body_type_ua=body_type_ua,
                country=d.get("country", "Europe"),
                location=d.get("city", ""),
                image=images[0] if images else None,
                gallery=images[1:] if len(images) > 1 else [],
            )
            car.score = calc_score(
                year=car.year, mileage=car.mileage_km,
                price_eur=car.price_eur, has_image=bool(car.image),
                has_features=False, hp=car.horsepower,
                gallery_count=len(car.gallery), feature_count=0,
                has_color=False, has_drive=False,
                has_body_type=bool(body_type),
            )
            cars.append(car)
        except Exception as e:
            logger.debug(f"[as24:http] Failed to parse listing: {e}")
            continue

    # Enrich a small number of cars with detail-page quality signals.
    # Each detail fetch = 1 HTTP roundtrip + rate_limiter; 8 cars ≈ 2s with
    # semaphore=8 concurrent. More than that → scrape timeout (35s).
    if cars:
        enrich_count = min(8, len(cars))
        await _enrich_top_results(cars, enrich_count)

    logger.info(f"[as24:http] {len(cars)} cars from {total} total ({min(8, len(cars))} enriched)")
    return cars


async def _enrich_top_results(cars: list[ParsedCar], count: int):
    """Fetch detail pages for top N cars to get color, drive, features, better photos."""
    import httpx as _httpx
    semaphore = asyncio.Semaphore(8)

    from .rate_limiter import acquire as _rl

    async def _enrich_one(car: ParsedCar):
        if not car.source_url:
            return
        async with semaphore:
            await _rl("autoscout24.com")  # respect rate limits on detail fetches too
            # curl_cffi preferred for TLS fingerprint (matches list page).
            client_ctx = (
                _CurlSession(impersonate="chrome136", headers=_HTTP_HEADERS, timeout=15)
                if _HAS_CURL_CFFI
                else _httpx.AsyncClient(headers=_HTTP_HEADERS, follow_redirects=True, timeout=15)
            )
            try:
                async with client_ctx as client:
                    d = await fetch_details(car.source_url, client)
                    if not d.get("make"):
                        return
                    # Fill missing fields from detail page
                    if not car.color and d.get("color"):
                        car.color = d["color"]
                        car.color_ua = d.get("color_ua")
                    if not car.drive and d.get("drive"):
                        car.drive = d["drive"]
                    if not car.body_type and d.get("body_type"):
                        car.body_type = d["body_type"]
                        car.body_type_ua = d.get("body_type_ua")
                    if not car.doors and d.get("doors"):
                        car.doors = d["doors"]
                    if not car.seats and d.get("seats"):
                        car.seats = d["seats"]
                    # Features
                    if d.get("safety_features"):
                        car.safety_features = d["safety_features"]
                    if d.get("comfort_features"):
                        car.comfort_features = d["comfort_features"]
                    if d.get("infotainment"):
                        car.infotainment = d["infotainment"]
                    # Quality signals — copy if extracted
                    for qkey in (
                        "previous_owners", "accident_free", "service_history",
                        "has_damage", "warranty_months", "seller_type", "inspection_valid_until",
                        # Extended spec/environment
                        "interior_color", "interior_color_ua", "emission_class",
                        "co2_emissions_g_km", "fuel_consumption_combined", "energy_efficiency",
                        "cylinders", "gears", "vin", "engine_cc", "weight_kg", "tow_capacity_kg",
                        # Dealer
                        "dealer_name", "dealer_rating", "dealer_review_count",
                        "dealer_phone", "dealer_website", "dealer_city", "dealer_zip",
                        # Sales/listing
                        "description", "title_line", "first_registration",
                        "video_url", "listing_updated_at",
                        # Also copy features_other in case detail has more
                    ):
                        v = d.get(qkey)
                        if v is not None:
                            setattr(car, qkey, v)
                    if d.get("features_other"):
                        car.features_other = d["features_other"]
                    # Better photos from detail page
                    detail_photos = d.get("photos", [])
                    if detail_photos and len(detail_photos) > len(car.gallery):
                        car.image = detail_photos[0]
                        car.gallery = detail_photos[1:]
                    # Recalculate score with enriched data + quality signals
                    from .base import count_premium_features
                    feat_count = len(car.safety_features or []) + len(car.comfort_features or []) + len(car.infotainment or [])
                    premium = count_premium_features(car.safety_features, car.comfort_features, car.infotainment, car.features_other)
                    car.score = calc_score(
                        year=car.year, mileage=car.mileage_km,
                        price_eur=car.price_eur, has_image=bool(car.image),
                        has_features=bool(car.safety_features or car.comfort_features),
                        hp=car.horsepower,
                        gallery_count=len(car.gallery),
                        feature_count=feat_count,
                        has_color=bool(car.color), has_drive=bool(car.drive),
                        has_body_type=bool(car.body_type),
                        previous_owners=car.previous_owners,
                        accident_free=car.accident_free,
                        service_history=car.service_history,
                        has_damage=car.has_damage,
                        warranty_months=car.warranty_months,
                        seller_type=car.seller_type,
                        premium_feature_count=premium,
                    )
            except Exception as e:
                logger.debug(f"[as24:enrich] {car.source_url}: {e}")

    # Enrich top N by score — prioritize cars missing data, but include top scorers too
    top = sorted(cars, key=lambda c: (
        0 if (not c.color or not c.drive or not c.safety_features) else 1,  # missing data first
        -c.score,  # then by score descending
    ))[:count]
    if top:
        await asyncio.gather(*(asyncio.create_task(_enrich_one(c)) for c in top))


def _parse_listing_data(listing: dict) -> dict:
    """
    Parse an AS24 listing dict into a flat data dict.
    Works with both __NEXT_DATA__ from HTML pages AND _next/data JSON responses.
    This is the SHARED parsing logic used by both HTTP-only and Playwright paths.
    """
    vehicle = listing.get("vehicle", {})
    result = {
        "make": None, "model": None, "title": None,
        "photos": [], "horsepower": None,
        "color": None, "color_ua": None,
        "drive": None, "doors": None, "seats": None,
        "body_type": None, "body_type_ua": None,
        "fuel": None, "fuel_ua": None,
        "transmission": None, "engine": None,
        "year": None, "mileage": None, "price": None,
        "safety_features": [], "comfort_features": [],
        "infotainment": [], "features_other": [],
        "source_url": None, "country": "Europe", "location": None,
    }

    # ── Source URL ────────────────────────────────────────────────────────
    result["source_url"] = (
        listing.get("url")
        or listing.get("detailPageUrl")
        or listing.get("detailUrl")
        or ""
    )
    if result["source_url"] and not result["source_url"].startswith("http"):
        result["source_url"] = f"{BASE_URL}{result['source_url']}"

    # ── Country ──────────────────────────────────────────────────────────
    _COUNTRY_CODE_MAP = {
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
    loc_data = listing.get("location", {}) or {}
    country_code = loc_data.get("countryCode") or ""
    if not country_code:
        seller = listing.get("seller", {}) or {}
        seller_addr = seller.get("address", {}) or {}
        country_code = seller_addr.get("countryCode") or ""
    result["country"] = _COUNTRY_CODE_MAP.get(country_code.upper(), "Europe") if country_code else "Europe"
    result["location"] = loc_data.get("city") or loc_data.get("zip") or ""

    # ── Make & Model ─────────────────────────────────────────────────────
    raw_make = vehicle.get("make", "")
    raw_model = vehicle.get("model", "") or vehicle.get("modelVersionInput", "")
    result["make"] = normalize_make(raw_make)
    result["model"] = normalize_model(raw_model, raw_make)
    result["title"] = f"{result['make']} {result['model']}"

    # ── Year ─────────────────────────────────────────────────────────────
    first_reg_raw = vehicle.get("firstRegistrationDateRaw") or ""
    first_reg = vehicle.get("firstRegistrationDate") or ""
    if first_reg_raw and len(first_reg_raw) >= 4:
        try:
            result["year"] = int(first_reg_raw[:4])
        except ValueError:
            pass
    if not result["year"] and first_reg:
        yr_m = re.search(r'(\d{4})', first_reg)
        if yr_m:
            result["year"] = int(yr_m.group(1))
    if not result["year"]:
        prod = vehicle.get("productionYear") or ""
        if prod:
            try:
                result["year"] = int(str(prod)[:4])
            except ValueError:
                pass

    # ── Mileage ──────────────────────────────────────────────────────────
    result["mileage"] = vehicle.get("mileageInKmRaw")
    if not result["mileage"]:
        for mileage_key in ("mileageInKm", "mileage", "mileageFormatted", "kilometerstand"):
            mileage_str = vehicle.get(mileage_key)
            if mileage_str:
                km_clean = re.sub(r'[^\d]', '', str(mileage_str))
                if km_clean:
                    result["mileage"] = int(km_clean)
                    break

    # ── Price ────────────────────────────────────────────────────────────
    prices = listing.get("prices", {})
    result["price"] = (
        (prices.get("public") or {}).get("priceRaw")
        or (prices.get("publicPrice") or {}).get("value")
        or (prices.get("price") or {}).get("value")
    )
    # Fallback for _next/data format
    if not result["price"]:
        price_obj = listing.get("price", {})
        if isinstance(price_obj, dict):
            result["price"] = price_obj.get("amount") or price_obj.get("value")
        elif isinstance(price_obj, (int, float)):
            result["price"] = price_obj

    # ── Fuel & Transmission ──────────────────────────────────────────────
    fuel_raw = vehicle.get("fuelCategory", {}).get("formatted") or vehicle.get("fuel", "")
    result["fuel"] = normalize_fuel(fuel_raw) or parse_fuel_from_text(fuel_raw)
    result["fuel_ua"] = FUEL_UA.get(result["fuel"] or "", None)

    trans_raw = vehicle.get("transmissionType", "") or vehicle.get("gearbox", "")
    result["transmission"] = normalize_transmission(trans_raw)

    # ── Engine ───────────────────────────────────────────────────────────
    displacement_cc = vehicle.get("rawDisplacementInCCM") or vehicle.get("rawCylinderCapacity")
    if displacement_cc and isinstance(displacement_cc, (int, float)):
        displacement_l = round(displacement_cc / 1000, 1)
        fuel_short = result.get("fuel") or ""
        result["engine"] = f"{displacement_l} {fuel_short}".strip() if fuel_short else f"{displacement_l}L"
    else:
        model_desc = vehicle.get("modelVersionInput", "") or listing.get("title", "")
        displacement_match = re.search(r'\b(\d\.\d)\s*(?:l|L|liter)?\b', str(model_desc))
        if displacement_match:
            fuel_short = result.get("fuel") or ""
            result["engine"] = f"{displacement_match.group(1)} {fuel_short}".strip()

    # ── Power ────────────────────────────────────────────────────────────
    hp = vehicle.get("rawPowerInHp")
    if not hp:
        kw = vehicle.get("rawPowerInKw")
        if kw:
            hp = int(kw * 1.341)
    result["horsepower"] = hp

    # ── Doors, Seats, Color, Drive, Body ─────────────────────────────────
    result["doors"] = vehicle.get("numberOfDoors")
    result["seats"] = vehicle.get("numberOfSeats")

    body_color = vehicle.get("bodyColor", "") or vehicle.get("bodyColorOriginal", "")
    if body_color:
        result["color"], result["color_ua"] = normalize_color(body_color)

    drive_raw = vehicle.get("driveTrain") or vehicle.get("driveType") or ""
    result["drive"] = normalize_drive(drive_raw)

    body_raw = vehicle.get("bodyType", "") or vehicle.get("bodyTypeGroup", "")
    if body_raw:
        result["body_type"], result["body_type_ua"] = normalize_body_type(body_raw)

    # ── Photos ───────────────────────────────────────────────────────────
    raw_images = listing.get("images", [])
    photos = []
    for img in raw_images:
        src = ""
        if isinstance(img, str):
            src = img
        elif isinstance(img, dict):
            src = img.get("url") or img.get("src") or ""
        if src:
            clean = re.sub(r"/\d+x\d+\.\w+$", "/800x600.jpg", src.split("?")[0])
            photos.append(clean)
    result["photos"] = photos

    # ── Equipment ────────────────────────────────────────────────────────
    equipment = vehicle.get("equipment", {})
    if not equipment:
        equipment = listing.get("equipment", {})

    CATEGORY_MAP = {
        "comfortAndConvenience": "comfort",
        "entertainmentAndMedia": "infotainment",
        "safetyAndSecurity": "safety",
        "extras": "other",
    }

    for eq_key, category in CATEGORY_MAP.items():
        items = equipment.get(eq_key, [])
        for item in items:
            name = ""
            if isinstance(item, dict):
                name = item.get("label") or item.get("name") or item.get("formatted") or item.get("id", "")
                if name and name == name.upper() and "_" in name:
                    name = name.replace("_", " ").title()
            elif isinstance(item, str):
                name = item
            name = decode_html(name).strip()
            if not name or len(name) < 2:
                continue
            target = f"{category}_features" if category != "infotainment" else "infotainment"
            if category == "other":
                target = "features_other"
            result[target].append(name)

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  DETAIL FETCHER — HTTP + __NEXT_DATA__ (legacy, used by Playwright path)
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_details(url: str, client: httpx.AsyncClient) -> dict:
    """
    Парсить деталі авто з __NEXT_DATA__ JSON.
    Це ГОЛОВНЕ джерело make/model та всіх деталей.
    """
    empty = {
        "make": None, "model": None, "title": None,
        "photos": [], "horsepower": None,
        "color": None, "color_ua": None,
        "drive": None, "doors": None, "seats": None,
        "body_type": None, "body_type_ua": None,
        "fuel": None, "fuel_ua": None,
        "transmission": None, "engine": None,
        "year": None, "mileage": None, "price": None,
        "safety_features": [], "comfort_features": [],
        "infotainment": [], "features_other": [],
        # Quality signals
        "previous_owners": None, "accident_free": None, "service_history": None,
        "has_damage": None, "warranty_months": None, "seller_type": None,
        "inspection_valid_until": None,
        # Extended spec
        "interior_color": None, "interior_color_ua": None,
        "emission_class": None, "co2_emissions_g_km": None,
        "fuel_consumption_combined": None, "energy_efficiency": None,
        "cylinders": None, "gears": None, "vin": None, "engine_cc": None,
        "weight_kg": None, "tow_capacity_kg": None,
        # Dealer
        "dealer_name": None, "dealer_rating": None, "dealer_review_count": None,
        "dealer_phone": None, "dealer_website": None,
        "dealer_city": None, "dealer_zip": None,
        # Sales
        "description": None, "title_line": None, "first_registration": None,
        "video_url": None, "listing_updated_at": None,
    }

    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"[as24:http] {resp.status_code} {url}")
            return empty

        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            resp.text, re.DOTALL,
        )
        if not m:
            logger.warning(f"[as24:http] __NEXT_DATA__ not found: {url}")
            return empty

        data = json.loads(m.group(1))
        listing = data["props"]["pageProps"]["listingDetails"]
        vehicle = listing.get("vehicle", {})

        result = dict(empty)

        # ── Seller country ────────────────────────────────────────────────
        _COUNTRY_CODE_MAP = {
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
        loc_data = listing.get("location", {}) or {}
        country_code = loc_data.get("countryCode") or ""
        if not country_code:
            # Fallback: seller address
            seller = listing.get("seller", {}) or {}
            seller_addr = seller.get("address", {}) or {}
            country_code = seller_addr.get("countryCode") or ""
        if not country_code:
            # Fallback: extract from URL (e.g. ...offers/bmw-...-a1b2c3?...&country=D)
            url_cc = re.search(r'/(?:offers|annonce)/[^?]+-([a-z]{2})-', url.lower())
            if url_cc:
                country_code = url_cc.group(1).upper()
        result["country"] = _COUNTRY_CODE_MAP.get(country_code.upper(), "Europe") if country_code else "Europe"
        result["location"] = loc_data.get("city") or loc_data.get("zip") or ""

        # ── Make & Model (100% reliable from structured data) ─────────────
        raw_make = vehicle.get("make", "")
        raw_model = vehicle.get("model", "") or vehicle.get("modelVersionInput", "")
        result["make"] = normalize_make(raw_make)
        result["model"] = normalize_model(raw_model, raw_make)
        result["title"] = f"{result['make']} {result['model']}"

        # ── Year, mileage, price ──────────────────────────────────────────
        # firstRegistrationDate = "05/2020", firstRegistrationDateRaw = "2020-05-01"
        first_reg_raw = vehicle.get("firstRegistrationDateRaw") or ""
        first_reg = vehicle.get("firstRegistrationDate") or ""
        if first_reg_raw and len(first_reg_raw) >= 4:
            try:
                result["year"] = int(first_reg_raw[:4])
            except ValueError:
                pass
        if not result["year"] and first_reg:
            # Try "MM/YYYY" format
            yr_m = re.search(r'(\d{4})', first_reg)
            if yr_m:
                result["year"] = int(yr_m.group(1))
        if not result["year"]:
            prod = vehicle.get("productionYear") or ""
            if prod:
                try:
                    result["year"] = int(str(prod)[:4])
                except ValueError:
                    pass

        # mileageInKmRaw is always an int; mileageInKm is a formatted string "77,841 km"
        result["mileage"] = vehicle.get("mileageInKmRaw")
        if not result["mileage"]:
            # Fallback: try multiple known keys
            for mileage_key in ("mileageInKm", "mileage", "mileageFormatted", "kilometerstand"):
                mileage_str = vehicle.get(mileage_key)
                if mileage_str:
                    km_clean = re.sub(r'[^\d]', '', str(mileage_str))
                    if km_clean:
                        result["mileage"] = int(km_clean)
                        break
        if not result["mileage"]:
            # Log vehicle keys to help discover correct mileage field
            logger.debug(f"[as24] Mileage not found. Vehicle keys: {sorted(vehicle.keys())}")

        # prices.public.priceRaw is the canonical price field
        prices = listing.get("prices", {})
        result["price"] = (
            (prices.get("public") or {}).get("priceRaw")
            or (prices.get("publicPrice") or {}).get("value")
            or (prices.get("price") or {}).get("value")
        )

        # ── Fuel & transmission ───────────────────────────────────────────
        fuel_raw = vehicle.get("fuelCategory", {}).get("formatted") or vehicle.get("fuel", "")
        result["fuel"] = normalize_fuel(fuel_raw) or parse_fuel_from_text(fuel_raw)
        result["fuel_ua"] = FUEL_UA.get(result["fuel"] or "", None)

        trans_raw = vehicle.get("transmissionType", "") or vehicle.get("gearbox", "")
        result["transmission"] = normalize_transmission(trans_raw)

        # Engine description (e.g. "2.0 Diesel") built from displacement + fuel
        displacement_cc = vehicle.get("rawDisplacementInCCM") or vehicle.get("rawCylinderCapacity")
        if displacement_cc and isinstance(displacement_cc, (int, float)):
            displacement_l = round(displacement_cc / 1000, 1)
            fuel_short = result.get("fuel") or ""
            result["engine"] = f"{displacement_l} {fuel_short}".strip() if fuel_short else f"{displacement_l}L"
        else:
            # Fallback: extract displacement from model description (e.g. "320d" → "2.0 Diesel")
            model_desc = vehicle.get("modelVersionInput", "") or listing.get("title", "")
            displacement_match = re.search(r'\b(\d\.\d)\s*(?:l|L|liter)?\b', str(model_desc))
            if displacement_match:
                fuel_short = result.get("fuel") or ""
                result["engine"] = f"{displacement_match.group(1)} {fuel_short}".strip()
            else:
                result["engine"] = None

        # ── Power ─────────────────────────────────────────────────────────
        hp = vehicle.get("rawPowerInHp")
        if not hp:
            kw = vehicle.get("rawPowerInKw")
            if kw:
                hp = int(kw * 1.341)
        result["horsepower"] = hp

        # ── Doors & seats ─────────────────────────────────────────────────
        result["doors"] = vehicle.get("numberOfDoors")
        result["seats"] = vehicle.get("numberOfSeats")

        # ── Color ─────────────────────────────────────────────────────────
        body_color = vehicle.get("bodyColor", "") or vehicle.get("bodyColorOriginal", "")
        if body_color:
            result["color"], result["color_ua"] = normalize_color(body_color)

        # ── Drive ─────────────────────────────────────────────────────────
        drive_raw = vehicle.get("driveTrain") or vehicle.get("driveType") or ""
        result["drive"] = normalize_drive(drive_raw)

        # ── Body type ─────────────────────────────────────────────────────
        body_raw = vehicle.get("bodyType", "") or vehicle.get("bodyTypeGroup", "")
        if body_raw:
            result["body_type"], result["body_type_ua"] = normalize_body_type(body_raw)

        # ── Photos — HIGHEST resolution available ──────────────────────────
        # AS24 CDN serves arbitrary resolutions: /480x360.webp, /800x600.webp,
        # /1920x1440.webp. Test showed /1920x1440.webp returns ~140-250KB each
        # (high quality). We upgrade to this ceiling size.
        raw_images = listing.get("images", []) or vehicle.get("images", [])
        photos = []
        for img in raw_images:
            src = ""
            if isinstance(img, str):
                src = img
            elif isinstance(img, dict):
                # Prefer explicit "hi-res" / "xxxl" keys if present
                for k in ("xxxl", "xxl", "xl", "l", "src", "url"):
                    v = img.get(k) if isinstance(img.get(k), str) else None
                    if v: src = v; break
            if src:
                # Strip query, upgrade path-suffix resolution to max.
                clean = src.split("?")[0]
                clean = re.sub(r"/\d+x\d+\.(webp|jpg|jpeg|png)$", r"/1920x1440.\1", clean)
                # Some AS24 URLs use format like /pic.jpg — leave as-is but
                # try the CDN's "original" extension fallback if .webp works.
                if not re.search(r"\.(webp|jpg|jpeg|png)$", clean):
                    clean += ".jpg"
                photos.append(clean)
        # Deduplicate while preserving order
        seen = set()
        result["photos"] = [p for p in photos if not (p in seen or seen.add(p))]

        # ── Equipment ─────────────────────────────────────────────────────
        equipment = vehicle.get("equipment", {})
        if not equipment:
            # Fallback: деякі оголошення мають equipment в listing root
            equipment = listing.get("equipment", {})

        CATEGORY_MAP = {
            "comfortAndConvenience": "comfort",
            "entertainmentAndMedia": "infotainment",
            "safetyAndSecurity": "safety",
            "extras": "other",
        }

        for eq_key, category in CATEGORY_MAP.items():
            items = equipment.get(eq_key, [])
            for item in items:
                name = ""
                if isinstance(item, dict):
                    # Пріоритет: label > name > formatted > id
                    name = (
                        item.get("label")
                        or item.get("name")
                        or item.get("formatted")
                        or item.get("id", "")
                    )
                    # Якщо id — це технічний код (UPPER_SNAKE), humanize
                    if name and name == name.upper() and "_" in name:
                        name = name.replace("_", " ").title()
                elif isinstance(item, str):
                    name = item

                name = decode_html(name).strip()
                if not name or len(name) < 2:
                    continue

                target = f"{category}_features" if category != "infotainment" else "infotainment"
                if category == "other":
                    target = "features_other"
                result[target].append(name)

        # ── Quality signals ───────────────────────────────────────────────
        # AS24 __NEXT_DATA__ includes these under vehicle.condition / vehicle.history.
        history = vehicle.get("vehicleHistory") or vehicle.get("history") or {}
        condition = vehicle.get("condition") or {}

        # Previous owners — AS24 uses `noOfPreviousOwners` (confirmed from schema dump)
        for key in ("noOfPreviousOwners", "numberOfPreviousOwners", "previousOwnersCount"):
            v = vehicle.get(key) or history.get(key)
            if isinstance(v, int) and v >= 0:
                result["previous_owners"] = v
                break
            if isinstance(v, str):
                digits = re.sub(r"[^\d]", "", v)
                if digits:
                    try:
                        result["previous_owners"] = int(digits)
                        break
                    except ValueError:
                        pass

        # Accident-free — AS24 has `hadAccident` (bool). If True → damaged. If False → accident-free.
        had_accident = vehicle.get("hadAccident")
        if isinstance(had_accident, bool):
            result["accident_free"] = not had_accident
            if had_accident:
                result["has_damage"] = True
        elif isinstance(condition, dict):
            dmg_status = condition.get("damageStatus")
            if dmg_status:
                s = str(dmg_status).lower()
                if "no_damage" in s or "accident_free" in s or "unfallfrei" in s:
                    result["accident_free"] = True
                elif "damaged" in s or "unfall" in s or "repaired" in s:
                    result["has_damage"] = True

        # Service history — AS24 uses `hasFullServiceHistory`
        for key in ("hasFullServiceHistory", "fullServiceHistory", "scheckheftgepflegt"):
            v = vehicle.get(key)
            if isinstance(v, bool):
                result["service_history"] = v
                break

        # TÜV / next inspection — AS24 uses `nextVehicleSafetyInspection`
        tuv = (vehicle.get("nextVehicleSafetyInspection") or
               vehicle.get("hu") or vehicle.get("huUntil"))
        if tuv:
            m_tuv = re.search(r"(\d{4})[\-\/]?(\d{2})?", str(tuv))
            if m_tuv:
                ym = m_tuv.group(1) + ("-" + m_tuv.group(2) if m_tuv.group(2) else "")
                result["inspection_valid_until"] = ym

        # Warranty (in months, if listed)
        warranty = vehicle.get("warrantyInMonths") or vehicle.get("warranty")
        if isinstance(warranty, int) and warranty > 0:
            result["warranty_months"] = warranty
        elif isinstance(warranty, str):
            mw = re.search(r"(\d+)", warranty)
            if mw:
                result["warranty_months"] = int(mw.group(1))

        # Seller type — dealer vs private
        seller = listing.get("seller", {}) or {}
        s_type = (seller.get("type") or seller.get("sellerType") or "").lower()
        if s_type:
            if "deal" in s_type or "pro" in s_type or "händler" in s_type:
                result["seller_type"] = "dealer"
            elif "priv" in s_type:
                result["seller_type"] = "private"

        # ── Dealer info — name, rating, phone, website, address ────────────
        if seller:
            result["dealer_name"] = seller.get("name") or seller.get("companyName") or None
            r = seller.get("rating") or seller.get("averageRating")
            if r is not None:
                try: result["dealer_rating"] = float(r)
                except (ValueError, TypeError): pass
            rc = seller.get("ratingCount") or seller.get("numberOfReviews")
            if rc is not None:
                try: result["dealer_review_count"] = int(rc)
                except (ValueError, TypeError): pass
            phone = seller.get("phone") or seller.get("phoneNumber") or (seller.get("contact", {}) or {}).get("phone")
            if phone: result["dealer_phone"] = str(phone)
            website = seller.get("homepage") or seller.get("website")
            if website: result["dealer_website"] = str(website)
            addr = seller.get("address") or {}
            if isinstance(addr, dict):
                result["dealer_city"] = addr.get("city") or None
                result["dealer_zip"] = addr.get("zip") or addr.get("zipCode") or None

        # ── Interior color — AS24 uses `upholsteryColor` + `upholstery` (material) ──
        interior = vehicle.get("upholsteryColor") or vehicle.get("interiorColor") or ""
        if interior:
            ic, ic_ua = normalize_color(interior)
            if ic:
                result["interior_color"] = ic
                result["interior_color_ua"] = ic_ua
        upholstery = vehicle.get("upholstery")
        if upholstery and isinstance(upholstery, str):
            # Translate common upholstery materials
            _uph = upholstery.lower()
            if "leather" in _uph or "leder" in _uph:
                result["seat_material"] = "Leather"
                result["seat_material_ua"] = "Шкіра"
            elif "alcantara" in _uph:
                result["seat_material"] = "Alcantara"
                result["seat_material_ua"] = "Алькантара"
            elif "cloth" in _uph or "fabric" in _uph or "stoff" in _uph:
                result["seat_material"] = "Fabric"
                result["seat_material_ua"] = "Тканина"

        # ── WLTP / emissions block — AS24 packs this in vehicle.wltp or primaryFuel ──
        wltp = vehicle.get("wltp") or {}
        primary_fuel = vehicle.get("primaryFuel") or {}

        # Emission class (Euro 6, etc.)
        ec_sources = [
            wltp.get("emissionClass"), wltp.get("emissionStandard"),
            vehicle.get("emissionClass"), vehicle.get("emissionStandard"),
            primary_fuel.get("emissionClass"),
        ]
        for ec in ec_sources:
            if ec:
                result["emission_class"] = str(ec)
                break

        # CO2 (g/km)
        co2_sources = [
            wltp.get("co2Emissions"), wltp.get("co2EmissionsInGPerKm"),
            primary_fuel.get("co2Emissions"),
            vehicle.get("co2Emission"),
        ]
        for co2 in co2_sources:
            if co2 is None: continue
            try:
                d = re.sub(r"[^\d]", "", str(co2))
                if d:
                    result["co2_emissions_g_km"] = int(d)
                    break
            except (ValueError, TypeError): pass

        # Combined fuel consumption (L/100km or kWh/100km)
        fc_obj = vehicle.get("fuelConsumptionCombined") or wltp.get("fuelConsumptionCombined") or primary_fuel.get("consumptionCombined")
        if isinstance(fc_obj, dict):
            fc_val = fc_obj.get("value") or fc_obj.get("raw") or fc_obj.get("formatted")
        else:
            fc_val = fc_obj
        if fc_val:
            m_fc = re.search(r"([\d.,]+)", str(fc_val).replace(",", "."))
            if m_fc:
                try: result["fuel_consumption_combined"] = float(m_fc.group(1))
                except ValueError: pass

        # Energy efficiency (A+/A/B/C/...)
        ee = wltp.get("energyEfficiencyClass") or vehicle.get("energyEfficiency")
        if ee: result["energy_efficiency"] = str(ee)

        # ── Engine details: cylinders, gears, VIN, weight ───────────────────
        # Cylinders: AS24 doesn't expose directly, only via rawCylinderCapacity (cm³ per cyl? no that's displacement).
        # Actually rawDisplacementInCCM is total engine volume in cc.
        cyl = vehicle.get("numberOfCylinders") or vehicle.get("cylinders")
        if cyl:
            try: result["cylinders"] = int(cyl)
            except (ValueError, TypeError): pass

        # Gears
        g = vehicle.get("gears") or vehicle.get("numberOfGears")
        if g:
            try: result["gears"] = int(g)
            except (ValueError, TypeError): pass

        # VIN — AS24 sometimes puts it under `identifier` dict
        identifier = vehicle.get("identifier") or {}
        if isinstance(identifier, dict):
            vin = identifier.get("vin") or identifier.get("VIN")
            if vin and isinstance(vin, str) and len(vin) >= 10:
                result["vin"] = vin.strip().upper()[:17]
        if not result["vin"]:
            vin2 = vehicle.get("vin") or listing.get("vin")
            if vin2 and isinstance(vin2, str) and len(vin2) >= 10:
                result["vin"] = vin2.strip().upper()[:17]

        # Weight — AS24 has `weight` (empty weight), `grossVehicleWeight`, `payload`, `maximumTowingWeight`
        w = vehicle.get("weight") or vehicle.get("weightInKg") or vehicle.get("emptyWeight") or vehicle.get("kerbWeight")
        if w:
            try: result["weight_kg"] = int(re.sub(r"[^\d]", "", str(w)))
            except (ValueError, TypeError): pass
        tow = vehicle.get("maximumTowingWeight") or vehicle.get("towCapacityInKg")
        if tow:
            try: result["tow_capacity_kg"] = int(re.sub(r"[^\d]", "", str(tow)))
            except (ValueError, TypeError): pass

        # For motorcycles: engine_cc (displacement in cc)
        cc_raw = vehicle.get("rawDisplacementInCCM")
        if cc_raw:
            try: result["engine_cc"] = int(cc_raw)
            except (ValueError, TypeError): pass

        # ── Description — AS24 uses `marketingDescription` ──────────────────
        desc = vehicle.get("marketingDescription") or listing.get("description") or listing.get("sellerNotes") or ""
        if desc and isinstance(desc, str):
            result["description"] = desc.strip()[:2000]

        # ── Title line + first registration for display ────────────────────
        title = listing.get("title") or f"{result.get('make', '')} {result.get('model', '')}".strip()
        if title:
            result["title_line"] = title
        if first_reg:
            result["first_registration"] = first_reg

        # ── Video URL (AS24 dealers often upload walk-around videos) ───────
        video = vehicle.get("video") or listing.get("video") or {}
        if isinstance(video, dict):
            result["video_url"] = video.get("src") or video.get("url") or None
        elif isinstance(video, str):
            result["video_url"] = video

        # ── Listing last-updated (freshness) ────────────────────────────────
        ts = listing.get("lastModified") or listing.get("modifiedDate") or listing.get("updatedAt")
        if ts:
            result["listing_updated_at"] = str(ts)[:25]

        total_feats = sum(len(result[k]) for k in [
            "safety_features", "comfort_features", "infotainment", "features_other"
        ])
        logger.debug(
            f"[as24:detail] {result['make']} {result['model']} "
            f"| hp={result['horsepower']} color={result['color']} "
            f"drive={result['drive']} doors={result['doors']} "
            f"body={result['body_type']} photos={len(photos)} feats={total_feats} "
            f"country={result['country']} location={result['location']}"
        )

    except Exception as e:
        logger.warning(f"[as24:detail] {url}: {e}")

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  CARD PARSER — витягує URL + базові дані з картки в списку
# ══════════════════════════════════════════════════════════════════════════════

async def _extract_card_url(card) -> Optional[tuple[str, str]]:
    """Повертає (url, external_id) або None."""
    try:
        for link in await card.query_selector_all("a"):
            href = await link.get_attribute("href") or ""
            if href and ("/offers/" in href or "/annonce/" in href):
                url = href if href.startswith("http") else BASE_URL + href
                ext_id = extract_uuid(url)
                if ext_id:
                    return url, ext_id

        # Fallback: UUID з HTML картки
        card_html = await card.inner_html()
        uuid_m = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            card_html, re.I,
        )
        if uuid_m:
            ext_id = uuid_m.group(1)
            return f"{BASE_URL}/offers/{ext_id}", ext_id
    except Exception:
        pass
    return None


async def _extract_card_basics(card) -> dict:
    """Витягує price, year, mileage з тексту картки (як fallback)."""
    basics = {"price": None, "year": None, "mileage": None, "image": None}

    try:
        text = await card.inner_text()

        # Price
        price_el = await card.query_selector("[data-testid='regular-price']")
        if price_el:
            raw = (await price_el.inner_text()).strip()
            clean = re.sub(r"[€$£\s]", "", raw)
            clean = re.sub(r"[,.](?=\d{3}(?!\d))", "", clean)
            clean = re.sub(r"[^\d.]", "", clean)
            try:
                basics["price"] = float(clean) if clean else None
            except ValueError:
                pass

        # Year
        date_m = re.search(r"\b(?:0[1-9]|1[0-2])/(20(?:0[0-9]|1[0-9]|2[0-6]))\b", text)
        if date_m:
            basics["year"] = int(date_m.group(1))
        else:
            yr_m = re.search(r"\b(20(?:0[5-9]|1[0-9]|2[0-6]))\b", text)
            if yr_m:
                basics["year"] = int(yr_m.group(1))

        # Mileage
        km_m = re.search(r"([\d]{1,3}(?:[,.\s]\d{3})*)\s*km\b", text, re.IGNORECASE)
        if km_m:
            clean_km = re.sub(r"[,.\s]", "", km_m.group(1))
            if clean_km.isdigit():
                val = int(clean_km)
                if val < 999999:
                    basics["mileage"] = val

        # Image
        img_el = await card.query_selector("img[src*='prod.pictures']")
        if img_el:
            basics["image"] = await img_el.get_attribute("src")

    except Exception:
        pass

    return basics


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN PARSE FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

async def parse(
    context: BrowserContext,
    params: dict,
    max_results: int = 20,
) -> list[ParsedCar]:
    """
    Парсить AutoScout24:
      Primary: HTTP-only via _next/data JSON API (10-50x faster, no browser)
      Fallback: Playwright for list + HTTP __NEXT_DATA__ for details
    """
    # ── HTTP-first path (no Playwright needed) ────────────────────────────
    try:
        cars = await parse_http(params, max_results)
        if cars:
            logger.info(f"[as24] HTTP-only path succeeded: {len(cars)} cars")
            return cars
        logger.info("[as24] HTTP path returned 0 cars, trying Playwright fallback")
    except Exception as e:
        logger.warning(f"[as24] HTTP path failed ({type(e).__name__}: {e}), falling back to Playwright")

    # ── Playwright fallback ───────────────────────────────────────────────
    price_to = params.get("price_to")
    all_cards: list[tuple[str, str, dict]] = []  # (url, ext_id, basics)

    # ── Step 1: Playwright — збираємо URL карток ──────────────────────────
    page = await context.new_page()
    await page.route(
        "**/*.{png,jpg,jpeg,gif,webp,svg,css,woff,woff2,ttf,eot,mp4,mp3}",
        lambda r: r.abort(),
    )

    try:
        for page_num in range(1, params.get("max_pages", 2) + 1):
            url = build_url(params, page_num)
            logger.info(f"[as24] Page {page_num}: {url}")

            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            # Wait for listing cards to appear (AS24 renders via React)
            try:
                await page.wait_for_selector(
                    "article, [data-testid*='listing'], [class*='cldt-']",
                    timeout=8000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(1500)

            cards = await page.query_selector_all("article[class*='cldt']")
            if not cards:
                cards = await page.query_selector_all("article[class*='listing']")
            if not cards:
                cards = await page.query_selector_all("article")
            if not cards:
                cards = await page.query_selector_all("[data-testid*='listing-item']")
            logger.info(f"[as24] Cards found: {len(cards)}")

            for card in cards:
                result = await _extract_card_url(card)
                if result:
                    card_url, ext_id = result
                    basics = await _extract_card_basics(card)
                    all_cards.append((card_url, ext_id, basics))

            await asyncio.sleep(1.5)
    finally:
        await page.close()

    # Дедуплікація
    seen: set[str] = set()
    unique_cards = []
    for card_url, ext_id, basics in all_cards:
        if ext_id not in seen:
            seen.add(ext_id)
            unique_cards.append((card_url, ext_id, basics))
    unique_cards = unique_cards[:max_results]

    logger.info(f"[as24] Unique cards: {len(unique_cards)}, fetching details via HTTP...")

    # ── Step 2: HTTP — деталі паралельно ──────────────────────────────────
    semaphore = asyncio.Semaphore(5)

    async with httpx.AsyncClient(headers=_HTTP_HEADERS, follow_redirects=True) as client:

        async def fetch_one(idx: int, card_url: str, ext_id: str, basics: dict) -> Optional[ParsedCar]:
            async with semaphore:
                try:
                    d = await fetch_details(card_url, client)

                    make = d.get("make") or ""
                    model = d.get("model") or ""

                    if not make:
                        logger.warning(f"[as24] No make from details: {card_url}")
                        return None

                    year = d.get("year") or basics.get("year")
                    mileage = d.get("mileage") or basics.get("mileage")
                    price = d.get("price") or basics.get("price")

                    if isinstance(price, str):
                        try:
                            price = float(price)
                        except (ValueError, TypeError):
                            price = None
                    if isinstance(mileage, str):
                        try:
                            mileage = int(mileage)
                        except (ValueError, TypeError):
                            mileage = None

                    photos = d.get("photos", [])
                    image = photos[0] if photos else basics.get("image")
                    gallery = photos[1:] if len(photos) > 1 else []

                    car = ParsedCar(
                        source_site="autoscout24.com",
                        source_url=card_url,
                        external_id=ext_id,
                        make=make,
                        model=model,
                        year=year,
                        price_eur=price,
                        mileage_km=mileage,
                        fuel=d.get("fuel"),
                        fuel_ua=d.get("fuel_ua"),
                        transmission=d.get("transmission"),
                        horsepower=d.get("horsepower"),
                        drive=d.get("drive"),
                        body_type=d.get("body_type"),
                        body_type_ua=d.get("body_type_ua"),
                        engine=d.get("engine"),
                        color=d.get("color"),
                        color_ua=d.get("color_ua"),
                        doors=d.get("doors"),
                        seats=d.get("seats"),
                        image=image,
                        gallery=gallery,
                        safety_features=d.get("safety_features", []),
                        comfort_features=d.get("comfort_features", []),
                        infotainment=d.get("infotainment", []),
                        features_other=d.get("features_other", []),
                        location=d.get("location"),
                        country=d.get("country", "Europe"),
                    )

                    car.score = calc_score(
                        year=car.year,
                        mileage=car.mileage_km,
                        price_eur=car.price_eur,
                        has_image=bool(car.image),
                        has_features=bool(car.safety_features or car.comfort_features),
                        hp=car.horsepower,
                        gallery_count=len(car.gallery or []),
                        feature_count=len(car.safety_features or []) + len(car.comfort_features or []) + len(car.infotainment or []),
                        has_color=bool(car.color),
                        has_drive=bool(car.drive),
                        has_body_type=bool(car.body_type),
                    )

                    logger.info(
                        f"[as24] [{idx+1}/{len(unique_cards)}] {car.make} {car.model} "
                        f"{car.year} | {car.price_eur}€ | {car.mileage_km}km | "
                        f"hp={car.horsepower} color={car.color} drive={car.drive} "
                        f"photos={len(gallery)+1 if image else 0}"
                    )
                    return car

                except Exception as e:
                    logger.warning(f"[as24:fetch_one] {card_url}: {type(e).__name__}: {e}")
                    return None

        tasks = [
            fetch_one(i, url, eid, basics)
            for i, (url, eid, basics) in enumerate(unique_cards)
        ]
        raw_results = await asyncio.gather(*tasks)

    # Фільтруємо None і сортуємо
    cars = [c for c in raw_results if isinstance(c, ParsedCar)]
    cars.sort(key=lambda x: x.score, reverse=True)

    logger.info(f"[as24] Done: {len(cars)} cars parsed")
    return cars


# ══════════════════════════════════════════════════════════════════════════════
#  TEST
# ══════════════════════════════════════════════════════════════════════════════

async def test_one(url: str):
    """Тестує fetch_details для одного URL."""
    async with httpx.AsyncClient(headers=_HTTP_HEADERS, follow_redirects=True) as client:
        d = await fetch_details(url, client)

    print("\n=== AS24 Detail Test ===")
    print(f"Make:      {d.get('make')}")
    print(f"Model:     {d.get('model')}")
    print(f"Year:      {d.get('year')}")
    print(f"Price:     {d.get('price')}")
    print(f"Mileage:   {d.get('mileage')}")
    print(f"Engine:    {d.get('engine')}")
    print(f"Fuel:      {d.get('fuel')}")
    print(f"Trans:     {d.get('transmission')}")
    print(f"HP:        {d.get('horsepower')}")
    print(f"Color:     {d.get('color')} / {d.get('color_ua')}")
    print(f"Drive:     {d.get('drive')}")
    print(f"Body:      {d.get('body_type')} / {d.get('body_type_ua')}")
    print(f"Doors:     {d.get('doors')}")
    print(f"Seats:     {d.get('seats')}")
    print(f"Photos:    {len(d.get('photos', []))}")
    print(f"Safety:    {len(d.get('safety_features', []))} items")
    print(f"Comfort:   {len(d.get('comfort_features', []))} items")
    print(f"Info:      {len(d.get('infotainment', []))} items")
    print(f"Other:     {len(d.get('features_other', []))} items")
    return d


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://www.autoscout24.com/offers/audi-a4-f975561b-a72c-41ec-ae8d-adf8974edccc"
    asyncio.run(test_one(url))