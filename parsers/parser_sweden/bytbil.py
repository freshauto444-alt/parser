# parsers/parser_sweden/bytbil.py
# Bytbil.com — fully HTTP → list[ParsedCar]. Extracted from parser_sweden.py.

import asyncio
import re
import json
from typing import Optional

import httpx
from loguru import logger

from ._common import _HEADERS
from ..base import (
    ParsedCar,
    normalize_make, normalize_model, normalize_color, normalize_body_type,
    normalize_drive, normalize_fuel, normalize_transmission,
    parse_fuel_from_text, extract_uuid, decode_html, clean_int,
    calc_score, sek_to_eur, is_known_brand,
    translate_and_categorize_features, FUEL_UA,
)

# ══════════════════════════════════════════════════════════════════════════════
#  BYTBIL.COM — fully HTTP
# ══════════════════════════════════════════════════════════════════════════════

def _parse_bytbil_detail(html: str, url: str) -> dict:
    """
    Парсить HTML деталей Bytbil.
    Витягує ВСЕ що доступно: dt/dd specs, dataLayer price, photos, equipment.
    """
    result = {
        "year": None, "mileage": None, "price_sek": None,
        "fuel": None, "transmission": None, "horsepower": None,
        "body_type": None, "body_type_ua": None,
        "color": None, "color_ua": None,
        "drive": None, "doors": None, "seats": None,
        "engine": None,
        "photos": [], "make": None, "model": None, "title": None,
        "all_features": [],  # raw features for translation
        # Extended spec
        "cylinders": None, "gears": None, "weight_kg": None, "tow_capacity_kg": None,
        "co2_emissions_g_km": None, "fuel_consumption_combined": None,
        "interior_color": None, "interior_color_ua": None,
        "vin": None, "emission_class": None, "description": None,
    }

    # ── dt/dd pairs ───────────────────────────────────────────────────────
    pairs = re.findall(r'<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>', html, re.DOTALL)
    spec: dict[str, str] = {}
    for dt, dd in pairs:
        key = re.sub(r'<[^>]+>', '', dt).strip().lower()
        val = re.sub(r'<[^>]+>', '', dd).replace('&#xA0;', ' ').replace('&nbsp;', ' ').strip()
        val = decode_html(val)
        if key and val:
            spec[key] = val

    # Year
    year_val = spec.get("årsmodell") or spec.get("year") or spec.get("modellår") or ""
    m = re.search(r'(20\d{2})', year_val)
    if m:
        result["year"] = int(m.group(1))

    # Mileage: Swedish "mil" = 10 km. Detect unit explicitly.
    mil_val = spec.get("miltal") or spec.get("körsträcka") or ""
    if mil_val:
        text_lower = mil_val.lower()
        is_mil = "mil" in text_lower and "km" not in text_lower
        is_km = "km" in text_lower
        clean = re.sub(r'[^\d]', '', mil_val)
        if clean:
            val = int(clean)
            if is_mil:
                result["mileage"] = val * 10
            elif is_km or val > 100000:
                result["mileage"] = val
            else:
                # Ambiguous (no unit keyword). Default to km — safer than guessing mil.
                # Swedish "mil" listings almost always include "mil" in the text.
                result["mileage"] = val

    # Fuel
    fuel_val = spec.get("drivmedel") or spec.get("bränsle") or ""
    result["fuel"] = normalize_fuel(fuel_val) or parse_fuel_from_text(fuel_val) or parse_fuel_from_text(html)

    # Transmission
    trans_val = spec.get("växellåda") or spec.get("transmission") or ""
    result["transmission"] = normalize_transmission(trans_val)

    # Color — from spec or dataLayer
    color_val = spec.get("färg") or spec.get("exteriörfärg") or spec.get("kulör") or ""
    if color_val:
        result["color"], result["color_ua"] = normalize_color(color_val)

    # Body type
    karosseri = spec.get("karosseri") or spec.get("fordonstyp") or ""
    if karosseri:
        result["body_type"], result["body_type_ua"] = normalize_body_type(karosseri)

    # Drive
    drive_val = spec.get("drivning") or spec.get("drift") or ""
    result["drive"] = normalize_drive(drive_val)

    # Doors
    doors_val = spec.get("dörrar") or spec.get("antal dörrar") or ""
    if doors_val:
        d_m = re.search(r'(\d)', doors_val)
        if d_m:
            result["doors"] = int(d_m.group(1))

    # Seats
    seats_val = spec.get("säten") or spec.get("antal säten") or spec.get("sittplatser") or ""
    if seats_val:
        s_m = re.search(r'(\d)', seats_val)
        if s_m:
            result["seats"] = int(s_m.group(1))

    # ── Extended spec from dt/dd ─────────────────────────────────────────
    # Bytbil exposes many useful fields in the spec dict — extract them all.

    # Engine cylinders
    cyl_val = spec.get("cylindrar") or spec.get("cylinder") or ""
    cm = re.search(r"(\d+)", cyl_val)
    if cm:
        try: result["cylinders"] = int(cm.group(1))
        except ValueError: pass

    # Gears
    gears_val = spec.get("växlar") or spec.get("antal växlar") or ""
    gm = re.search(r"(\d+)", gears_val)
    if gm:
        try: result["gears"] = int(gm.group(1))
        except ValueError: pass

    # Weight (kg)
    weight_val = spec.get("tjänstevikt") or spec.get("vikt") or ""
    wm = re.search(r"(\d+)\s*(?:kg)?", weight_val.replace(" ", ""))
    if wm:
        try: result["weight_kg"] = int(wm.group(1))
        except ValueError: pass

    # Tow capacity
    tow_val = spec.get("max släpvagnsvikt") or spec.get("släp") or ""
    tm = re.search(r"(\d+)\s*(?:kg)?", tow_val.replace(" ", ""))
    if tm:
        try: result["tow_capacity_kg"] = int(tm.group(1))
        except ValueError: pass

    # CO2 emissions (g/km)
    co2_val = spec.get("co2") or spec.get("co2-utsläpp") or spec.get("koldioxidutsläpp") or ""
    co2_m = re.search(r"(\d+)", co2_val)
    if co2_m:
        try: result["co2_emissions_g_km"] = int(co2_m.group(1))
        except ValueError: pass

    # Fuel consumption (L/100km)
    fc_val = spec.get("bränsleförbrukning") or spec.get("förbrukning") or ""
    fc_m = re.search(r"([\d.,]+)", fc_val.replace(",", "."))
    if fc_m:
        try: result["fuel_consumption_combined"] = float(fc_m.group(1))
        except ValueError: pass

    # Interior color
    int_color = spec.get("invändig färg") or spec.get("inredning") or ""
    if int_color:
        ic, ic_ua = normalize_color(int_color)
        if ic:
            result["interior_color"] = ic
            result["interior_color_ua"] = ic_ua

    # VIN
    vin_val = spec.get("chassinummer") or spec.get("vin") or ""
    if vin_val and len(vin_val) >= 10:
        result["vin"] = re.sub(r"[^A-Z0-9]", "", vin_val.upper())[:17]

    # Emission class
    em_val = spec.get("euroklass") or spec.get("miljöklass") or ""
    if em_val:
        result["emission_class"] = em_val.strip()

    # Description — Bytbil usually has a <div class="description"> or similar.
    desc_m = re.search(r'<div[^>]*class="[^"]*description[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
    if desc_m:
        d = decode_html(re.sub(r'<[^>]+>', ' ', desc_m.group(1))).strip()
        d = re.sub(r'\s+', ' ', d)
        if len(d) > 20:
            result["description"] = d[:2000]

    # ── Quality signals ───────────────────────────────────────────────────
    # Previous owners — "Ägare"/"Antal ägare"
    owners_val = spec.get("ägare") or spec.get("antal ägare") or ""
    if owners_val:
        om = re.search(r'(\d+)', owners_val)
        if om:
            try:
                result["previous_owners"] = int(om.group(1))
            except ValueError:
                pass

    # Service history — "Servicebok"/"Servicerapport"/"Fullst. servicebok"
    svc = spec.get("servicebok") or spec.get("servicerapport") or ""
    html_lower = html.lower()
    if svc or "fullst" in html_lower and "servicebok" in html_lower:
        # Any "ja"/"yes"/"fullst." → full service book
        svc_l = (svc or "").lower()
        if "ja" in svc_l or "yes" in svc_l or "fullst" in svc_l or "komplett" in svc_l:
            result["service_history"] = True

    # Inspection (besiktning) — "Besiktigad"/"Besiktning"
    bes_val = spec.get("besiktigad") or spec.get("besiktning") or spec.get("nästa besiktning") or ""
    bm = re.search(r'(20\d{2})[\-\s\.\/]?(\d{2})?', bes_val or "")
    if bm:
        ym = bm.group(1) + ("-" + bm.group(2) if bm.group(2) else "")
        result["inspection_valid_until"] = ym

    # Accident-free — Swedish "olycksfri" / "skadefri" / condition text
    cond_val = (spec.get("skick") or spec.get("kondition") or "").lower()
    if "olycksfri" in cond_val or "skadefri" in cond_val or "olycksfri" in html_lower[:20000]:
        result["accident_free"] = True
    elif "krockskadad" in cond_val or "skadad" in cond_val or "reparerad" in cond_val:
        result["has_damage"] = True

    # ── Price from dataLayer / meta / text ────────────────────────────────
    price_patterns = [
        r"dataLayer\.push\s*\(\s*\{.*?'price'\s*:\s*'(\d+)'",
        r'"price"\s*:\s*"(\d+)"',
        r'"price"\s*:\s*(\d+)',
        r'(\d[\d\s]{3,})\s*kr\b',
    ]
    for pattern in price_patterns:
        pm = re.search(pattern, html, re.DOTALL)
        if pm:
            try:
                price_str = re.sub(r'\s', '', pm.group(1))
                result["price_sek"] = float(price_str)
                break
            except (ValueError, IndexError):
                continue

    # ── Horsepower ────────────────────────────────────────────────────────
    hp_val = spec.get("effekt") or spec.get("motoreffekt") or ""
    hp_m = re.search(r'(\d{2,4})\s*hk', hp_val or html, re.IGNORECASE)
    if hp_m:
        result["horsepower"] = int(hp_m.group(1))
    else:
        kw_m = re.search(r'(\d{2,3})\s*kw', hp_val or html, re.IGNORECASE)
        if kw_m:
            result["horsepower"] = int(int(kw_m.group(1)) * 1.341)

    # ── Engine ────────────────────────────────────────────────────────────
    # Try spec keys first, then extract decimal displacement from spec text.
    # NOTE: never search raw html[:5000] — it contains "initial-scale=1.0" in
    # the viewport meta tag which always matches the displacement regex.
    engine_spec = spec.get("motor") or spec.get("motorvolym") or spec.get("cylindervolym") or ""
    if engine_spec:
        result["engine"] = engine_spec
    else:
        # Search only within the spec dict values (safe, no viewport meta noise)
        spec_text = " ".join(spec.values())
        eng_m = re.search(
            r'(?<![=:])\b([1-9]\.\d)\s*(?:TFSI|TDI|TSI|FSI|CDI|HDi|dCi|T\b|liter\b|l\b)',
            spec_text, re.IGNORECASE,
        )
        if not eng_m:
            # Also try the page title / h1 which is safe
            title_area = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL | re.IGNORECASE)
            if title_area:
                eng_m = re.search(
                    r'\b([1-9]\.\d)\b', title_area.group(1), re.IGNORECASE
                )
        if eng_m:
            fuel_short = result.get("fuel") or ""
            result["engine"] = f"{eng_m.group(1)} {fuel_short}".strip()

    # ── Photos — keep CDN URL as-is from the page ────────────────────────
    # Bytbil uses BigBuy CDN (bbcdn.io). The `rule=legacy-<preset>` query
    # chooses resolution. We previously upgraded to `legacy-hires` for 1600px
    # quality, but that preset is no longer valid (CDN returns 404 with
    # `X-Reason: Invalid Rule`). Use whatever rule the page provides.
    seen: set = set()
    photos = []
    for src in re.findall(r'https://[^"\'>\s]*bbcdn\.io[^"\'>\s]+', html):
        if any(k in src.lower() for k in ["logo", "vend_logo", "finance", "svg"]):
            continue
        clean = src.split('"')[0].split("'")[0]
        if clean not in seen:
            seen.add(clean)
            photos.append(clean)
    result["photos"] = photos[:40]

    # ── Equipment list ────────────────────────────────────────────────────
    all_features = []

    # Method 1: equipment-list <ul>
    eq_start = html.find('equipment-list')
    if eq_start > 0:
        ul_start = html.rfind('<ul', 0, eq_start)
        ul_end = html.find('</ul>', eq_start)
        if ul_start > 0 and ul_end > 0:
            section = html[ul_start:ul_end + 5]
            items = re.findall(r'<li[^>]*>(.*?)</li>', section, re.DOTALL)
            for item in items:
                name = decode_html(re.sub(r'<[^>]+>', '', item)).strip()
                if name and len(name) > 2:
                    all_features.append(name)

    # Method 2: data attributes or JSON in page
    feat_json_m = re.search(r'"equipment"\s*:\s*\[(.*?)\]', html, re.DOTALL)
    if feat_json_m and not all_features:
        try:
            raw = "[" + feat_json_m.group(1) + "]"
            feat_list = json.loads(raw)
            for f in feat_list:
                if isinstance(f, str) and len(f) > 2:
                    all_features.append(decode_html(f))
                elif isinstance(f, dict):
                    name = f.get("name") or f.get("label") or ""
                    if name and len(name) > 2:
                        all_features.append(decode_html(name))
        except (json.JSONDecodeError, Exception):
            pass

    result["all_features"] = all_features

    # ── Title ─────────────────────────────────────────────────────────────
    title_m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
    if title_m:
        result["title"] = decode_html(re.sub(r'<[^>]+>', '', title_m.group(1)))

    return result


async def _fetch_bytbil_detail(url: str, client: httpx.AsyncClient) -> dict:
    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code == 200:
            return _parse_bytbil_detail(resp.text, url)
    except Exception as e:
        logger.warning(f"[bytbil:detail] {url}: {e}")
    return {"year": None, "mileage": None, "price_sek": None,
            "fuel": None, "transmission": None, "photos": [], "title": None,
            "all_features": []}


async def parse_bytbil(
    context=None,  # UNUSED — Bytbil is fully HTTP. Kept for backward compat.
    sek_to_eur_rate: float = 0.088,
    brand: str = "",
    model: str = "",
    year_from: int = 2018,
    year_to: Optional[int] = None,
    price_to_eur: Optional[int] = None,
    price_from_eur: Optional[int] = None,
    max_results: int = 30,
    fuel: Optional[str] = None,
    transmission: Optional[str] = None,
    body_type: Optional[str] = None,
    vehicle_type: str = "Car",
) -> list[ParsedCar]:
    """Fully HTTP: list page → detail pages.

    Bytbil path per vehicle_type:
      Car → /bil?VehicleType=bil
      Motorcycle → /mc?VehicleType=mc
      Truck/Van/Bus → /transportbil?VehicleType=transportbil
      Camper → /husbil?VehicleType=husbil
    """

    _fuel_map = {
        "petrol": "Bensin", "diesel": "Diesel",
        "electric": "El", "hybrid": "Laddhybrid",
    }
    _body_map = {
        "suv": "SUV", "sedan": "Sedan", "hatchback": "Halvkombi",
        "estate": "Kombi", "coupe": "Coupe", "convertible": "Cab",
        "van": "Minibuss", "pickup": "Pickup",
    }
    _trans_map = {"automatic": "Automat", "manual": "Manuell"}

    # Map vehicle_type → Bytbil URL segment + VehicleType param
    _vt_map = {
        "Car": ("bil", "bil"),
        "Motorcycle": ("mc", "mc"),
        "Truck": ("transportbil", "transportbil"),
        "Van": ("transportbil", "transportbil"),
        "Bus": ("transportbil", "transportbil"),
        "Camper": ("husbil", "husbil"),
    }
    path_seg, vt_param = _vt_map.get(vehicle_type, ("bil", "bil"))

    make_param = brand.title() if brand else ""
    model_param = model.title() if model else ""
    price_sek_to = int(price_to_eur / sek_to_eur_rate) if price_to_eur else ""
    price_sek_from = int(price_from_eur / sek_to_eur_rate) if price_from_eur else ""
    fuel_param = _fuel_map.get((fuel or "").split(",")[0].strip().lower(), "") if fuel else ""
    body_param = _body_map.get((body_type or "").split(",")[0].strip().lower(), "") if body_type else ""
    trans_param = _trans_map.get((transmission or "").lower(), "")

    year_to_param = year_to or ""
    # Price-relevant sort when user has explicit price range — gives more in-range
    # cars per page than newest-first (which is mostly leasing/configurator listings).
    has_price_range = bool(price_sek_from or price_sek_to)
    sort_field = "price" if has_price_range else "publishedDate"
    sort_asc = "True" if has_price_range else "False"
    list_url = (
        f"https://www.bytbil.com/{path_seg}"
        f"?VehicleType={vt_param}"
        f"&Makes={make_param}"
        f"&FreeText={model_param}"
        f"&ModelYearRange.From={year_from}"
        f"&ModelYearRange.To={year_to_param}"
        f"&PriceRange.From={price_sek_from}"
        f"&PriceRange.To={price_sek_to}"
        f"&BodyTypes={body_param}"
        f"&Gearboxes={trans_param}"
        f"&FuelTypes={fuel_param}"
        f"&SortParams.SortField={sort_field}&SortParams.IsAscending={sort_asc}"
    )
    logger.info(f"[bytbil] {list_url}")

    deals: list[ParsedCar] = []

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        # ── Fetch list pages (paginate until enough URLs or no new ones) ──
        from .rate_limiter import acquire as _rl
        # Determine how many pages to fetch — 1 page yields ~24 URLs.
        pages_to_fetch = max(1, (max_results + 23) // 24)
        pages_to_fetch = min(pages_to_fetch, 3)  # safety cap

        # Extract model tokens for flexible matching. Bytbil slugs use Swedish naming
        # ("3-serien" for BMW 3 Series, "x5" for BMW X5) — exact slug match fails.
        # Instead: match if ANY token from the model name appears in the URL slug.
        model_tokens = [t for t in re.split(r'[\s\-/]+', (model or "").lower()) if len(t) >= 2]
        # Drop generic "series"/"class" tokens (too common, skip)
        model_tokens = [t for t in model_tokens if t not in {"series", "serie", "class", "klasse", "klass"}]

        seen_urls: set = set()
        unique_paths: list[str] = []

        for page_num in range(1, pages_to_fetch + 1):
            page_url = list_url if page_num == 1 else f"{list_url}&Page={page_num}"
            try:
                await _rl("bytbil.com")
                resp = await client.get(page_url, timeout=20)
                html = resp.text
            except Exception as e:
                logger.error(f"[bytbil] list page {page_num} error: {e}")
                break

            listing_urls = re.findall(r'href="(/[a-zåäö0-9\-]+/personbil[^"?#]+)"', html)
            page_added = 0
            for p in listing_urls:
                if p in seen_urls:
                    continue
                seen_urls.add(p)
                if model_tokens:
                    p_lower = p.lower()
                    if not any(tok in p_lower for tok in model_tokens):
                        continue
                unique_paths.append(p)
                page_added += 1

            # If page added 0 new URLs, no point fetching further pages
            if page_added == 0:
                break
            if len(unique_paths) >= max_results:
                break

        logger.info(f"[bytbil] URLs found: {len(unique_paths)} (across {page_num} page(s))")
        unique_paths = unique_paths[:max_results]

        # ── Detail pages in parallel ──────────────────────────────────────
        semaphore = asyncio.Semaphore(5)

        async def fetch_one(path: str, idx: int) -> Optional[ParsedCar]:
            async with semaphore:
                url = f"https://www.bytbil.com{path}"
                d = await _fetch_bytbil_detail(url, client)

                title = d.get("title") or path.split("/")[-1].replace("-", " ").title()
                words = title.split()
                if len(words) < 2:
                    return None

                # Make from title
                make = None
                for w in words:
                    if is_known_brand(w):
                        make = normalize_make(w)
                        break
                if not make:
                    return None

                # Model from title: take words after make, stop at promotional garbage
                # e.g. "Audi A6 TOVEKSDAGARNA 4,95% Avant..." → "A6"
                make_word_count = len(make.split()) if make else 1
                model_words_raw = words[make_word_count:]
                clean_model_words = []
                for w in model_words_raw:
                    # Stop at ALL-CAPS promo words (>4 chars) or words with %
                    if "%" in w:
                        break
                    if len(w) > 4 and w.upper() == w and not re.match(r'^[A-Z0-9\-]+$', w):
                        break
                    clean_model_words.append(w)
                model_name = " ".join(clean_model_words[:3]) if clean_model_words else ""
                # Fallback: extract model from URL slug (most reliable)
                if not model_name:
                    slug = path.split("/")[-1]  # e.g. "personbil-a6-avant-55-..."
                    if slug.startswith("personbil-"):
                        first_slug_word = slug[len("personbil-"):].split("-")[0]
                        model_name = first_slug_word.upper()
                model_name = normalize_model(model_name, make)

                year = d.get("year")
                if not year:
                    ym = re.search(r'-(20[12]\d)-', path)
                    if ym:
                        year = int(ym.group(1))

                price_sek = d.get("price_sek")
                price_eur = round(price_sek * sek_to_eur_rate) if price_sek else None

                photos = d.get("photos", [])

                # Translate & categorize all features
                raw_features = d.get("all_features", [])
                feats = translate_and_categorize_features(raw_features)

                car = ParsedCar(
                    source_site="bytbil.com",
                    source_url=url,
                    external_id=path.split("-")[-1],
                    make=make,
                    model=model_name,
                    year=year,
                    price_eur=price_eur,
                    price_original=price_sek,
                    price_currency="SEK",
                    mileage_km=d.get("mileage"),
                    fuel=d.get("fuel"),
                    fuel_ua=FUEL_UA.get(d.get("fuel") or "", None),
                    transmission=d.get("transmission"),
                    horsepower=d.get("horsepower"),
                    engine=d.get("engine"),
                    drive=d.get("drive"),
                    body_type=d.get("body_type"),
                    body_type_ua=d.get("body_type_ua"),
                    color=d.get("color"),
                    color_ua=d.get("color_ua"),
                    doors=d.get("doors"),
                    seats=d.get("seats"),
                    image=photos[0] if photos else None,
                    gallery=photos[1:] if len(photos) > 1 else [],
                    safety_features=feats["safety"],
                    comfort_features=feats["comfort"],
                    infotainment=feats["infotainment"],
                    features_other=feats["other"],
                    country="Sweden",
                    previous_owners=d.get("previous_owners"),
                    accident_free=d.get("accident_free"),
                    service_history=d.get("service_history"),
                    has_damage=d.get("has_damage"),
                    inspection_valid_until=d.get("inspection_valid_until"),
                    # Extended spec (Bytbil)
                    cylinders=d.get("cylinders"),
                    gears=d.get("gears"),
                    weight_kg=d.get("weight_kg"),
                    tow_capacity_kg=d.get("tow_capacity_kg"),
                    co2_emissions_g_km=d.get("co2_emissions_g_km"),
                    fuel_consumption_combined=d.get("fuel_consumption_combined"),
                    interior_color=d.get("interior_color"),
                    interior_color_ua=d.get("interior_color_ua"),
                    vin=d.get("vin"),
                    emission_class=d.get("emission_class"),
                    description=d.get("description"),
                    title_line=d.get("title"),
                )
                from .base import count_premium_features
                premium = count_premium_features(car.safety_features, car.comfort_features, car.infotainment, car.features_other)
                car.score = calc_score(
                    year=car.year, mileage=car.mileage_km,
                    price_eur=car.price_eur, has_image=bool(car.image),
                    has_features=bool(raw_features), hp=car.horsepower,
                    gallery_count=len(car.gallery or []),
                    feature_count=len(car.safety_features or []) + len(car.comfort_features or []) + len(car.infotainment or []),
                    has_color=bool(car.color),
                    has_drive=bool(car.drive),
                    has_body_type=bool(car.body_type),
                    previous_owners=car.previous_owners,
                    accident_free=car.accident_free,
                    service_history=car.service_history,
                    has_damage=car.has_damage,
                    premium_feature_count=premium,
                )

                logger.debug(
                    f"[bytbil] [{idx+1}/{len(unique_paths)}] {car.make} {car.model} "
                    f"| {price_eur}€ | {year} | feats={len(raw_features)}"
                )
                return car

        tasks = [fetch_one(path, i) for i, path in enumerate(unique_paths)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        deals = [d for d in results if isinstance(d, ParsedCar)]

        if brand:
            deals = [d for d in deals if brand.lower() in d.make.lower()]

    logger.info(f"[bytbil] Done: {len(deals)} cars")
    return deals


