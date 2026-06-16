# parsers/parser_sweden/blocket_html.py
# LEGACY Blocket.se scraper (Playwright list + HTTP detail). Superseded by the
# mobility API (blocket_api.py) — parse_blocket is currently NOT called anywhere.
# Preserved in case the API path needs a fallback.

import asyncio
import re
import json
from typing import Optional

import httpx
from loguru import logger
from playwright.async_api import BrowserContext

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
#  BLOCKET.SE — Playwright list + HTTP details
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_blocket_detail(url: str, client: httpx.AsyncClient) -> dict:
    """
    Парсить деталі оголошення Blocket через HTTP.
    Витягує ld+json (повні структуровані дані) + фото.
    """
    result = {
        "year": None, "mileage": None, "price_sek": None,
        "fuel": None, "transmission": None, "horsepower": None,
        "body_type": None, "body_type_ua": None,
        "color": None, "color_ua": None,
        "drive": None, "doors": None, "seats": None,
        "photos": [], "make": None, "model": None, "title": None,
        "all_features": [],
        # Quality signals
        "previous_owners": None, "accident_free": None, "service_history": None,
        "has_damage": None, "inspection_valid_until": None,
        # Extended spec
        "cylinders": None, "gears": None, "weight_kg": None,
        "co2_emissions_g_km": None, "interior_color": None, "interior_color_ua": None,
        "vin": None, "emission_class": None, "description": None,
    }

    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code != 200:
            return result
        html = resp.text
        item_id = url.rstrip("/").split("/")[-1]

        # ── Quality signals from description text (before structured parse) ──
        # Blocket sellers often describe history in free text. Look for Swedish
        # phrases that indicate good/bad history.
        html_lower = html.lower()
        if "olycksfri" in html_lower or "aldrig krockad" in html_lower or "inga skador" in html_lower:
            result["accident_free"] = True
        elif "krockskadad" in html_lower or "skadad" in html_lower and "krock" in html_lower:
            result["has_damage"] = True
        if "fullständig service" in html_lower or "fullst. servicebok" in html_lower or "alla servicar" in html_lower or "servad hos" in html_lower:
            result["service_history"] = True
        # Previous owners: "1 ägare", "första ägare", "2:a ägare"
        own_m = re.search(r'(\d+)\s*ägare', html_lower)
        if own_m:
            try:
                result["previous_owners"] = int(own_m.group(1))
            except ValueError:
                pass
        # Inspection date
        bes_m = re.search(r'bes(?:ik|iktigad|iktning)[^<]{0,50}?(20\d{2})[\-\s\.\/]?(\d{2})?', html_lower)
        if bes_m:
            ym = bes_m.group(1) + ("-" + bes_m.group(2) if bes_m.group(2) else "")
            result["inspection_valid_until"] = ym

        # ── __NEXT_DATA__ — Next.js embedded JSON (most reliable) ────────────
        nd_m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if nd_m:
            try:
                nd = json.loads(nd_m.group(1))
                pp = nd.get("props", {}).get("pageProps", {})
                ad_data = (pp.get("adDetails") or pp.get("ad") or pp.get("item")
                           or pp.get("listing") or pp.get("car") or {})
                if not ad_data and "initialState" in nd:
                    ad_data = nd["initialState"].get("ad", {}) or {}

                def _nd_str(keys: list, d: dict) -> str:
                    for k in keys:
                        v = d.get(k)
                        if v:
                            if isinstance(v, dict):
                                v = v.get("name") or v.get("value") or v.get("label") or ""
                            return str(v) if v else ""
                    return ""

                # Price
                price_raw = _nd_str(["price", "askingPrice", "currentBid"], ad_data)
                if not price_raw:
                    price_raw = str((ad_data.get("offer") or {}).get("price") or "")
                if price_raw:
                    try:
                        result["price_sek"] = float(re.sub(r'[^\d.]', '', price_raw))
                    except (ValueError, TypeError):
                        pass

                # Make / model
                result["make"] = _nd_str(["make", "brand", "manufacturer"], ad_data)
                result["model"] = _nd_str(["model", "modelName", "name"], ad_data)

                # Year
                yr = _nd_str(["year", "modelYear", "registrationYear"], ad_data)
                ym2 = re.search(r'(20\d{2})', yr)
                if ym2:
                    result["year"] = int(ym2.group(1))

                # Mileage: detect Swedish "mil" vs km explicitly
                mil_raw = _nd_str(["mileage", "miltal", "odometer"], ad_data)
                if mil_raw:
                    raw_str = str(mil_raw).lower()
                    is_mil = "mil" in raw_str and "km" not in raw_str
                    is_km = "km" in raw_str
                    km2 = clean_int(re.sub(r'[^\d]', '', str(mil_raw)))
                    if km2:
                        if is_mil:
                            result["mileage"] = km2 * 10
                        elif is_km or km2 > 100000:
                            result["mileage"] = km2
                        elif km2 < 500:
                            result["mileage"] = km2 * 10  # likely Swedish mil
                        else:
                            result["mileage"] = km2

                # Fuel / transmission / horsepower
                fuel_raw2 = _nd_str(["fuel", "fuelType", "drivmedel"], ad_data)
                if fuel_raw2:
                    result["fuel"] = normalize_fuel(fuel_raw2)
                gearbox_raw = _nd_str(["gearbox", "transmission", "växellåda"], ad_data)
                if gearbox_raw:
                    result["transmission"] = normalize_transmission(gearbox_raw)
                hp_raw = _nd_str(["horsepower", "power", "hästkrafter", "effekt"], ad_data)
                if hp_raw:
                    hp2 = clean_int(re.sub(r'[^\d]', '', hp_raw))
                    if hp2 and hp2 < 2000:
                        result["horsepower"] = hp2

                # Body / color / drive
                body_raw2 = _nd_str(["bodyType", "body", "karosseri", "carType"], ad_data)
                if body_raw2:
                    result["body_type"], result["body_type_ua"] = normalize_body_type(body_raw2)
                color_raw2 = _nd_str(["color", "colour", "färg", "exteriorColor", "bodyColor"], ad_data)
                if not color_raw2:
                    # Try nested vehicle/car object
                    for sub_key in ("vehicle", "car", "ad", "item"):
                        sub = ad_data.get(sub_key, {})
                        if isinstance(sub, dict):
                            color_raw2 = sub.get("color") or sub.get("färg") or sub.get("exteriorColor") or ""
                            if color_raw2:
                                break
                if color_raw2:
                    result["color"], result["color_ua"] = normalize_color(color_raw2)
                drive_raw2 = _nd_str(["driveType", "drive", "drivning", "drivetrain"], ad_data)
                if drive_raw2:
                    result["drive"] = normalize_drive(drive_raw2)

                # Doors / seats
                doors_raw = _nd_str(["doors", "numberOfDoors", "dörrar"], ad_data)
                if doors_raw:
                    d2 = clean_int(re.sub(r'[^\d]', '', doors_raw))
                    if d2:
                        result["doors"] = d2
                seats_raw = _nd_str(["seats", "numberOfSeats", "säten"], ad_data)
                if seats_raw:
                    s2 = clean_int(re.sub(r'[^\d]', '', seats_raw))
                    if s2:
                        result["seats"] = s2

                # Images from __NEXT_DATA__ — normalize to /dynamic/1280w/.
                # `1600w` was tried earlier but Blocket's CDN rejects it (404).
                # 1280w is the largest preset confirmed working.
                imgs_nd = ad_data.get("images") or ad_data.get("photos") or []
                if isinstance(imgs_nd, list):
                    for img in imgs_nd:
                        src = (img.get("url") or img.get("src") or img.get("contentUrl")
                               or (img if isinstance(img, str) else ""))
                        if src:
                            result["photos"].append(re.sub(r'/dynamic/[^/]+/', '/dynamic/1280w/', src))

                # Description (free-text from seller)
                desc = _nd_str(["description", "bodyText", "text"], ad_data)
                if desc and len(desc) > 20:
                    # Strip HTML tags if any
                    d = re.sub(r'<[^>]+>', ' ', desc)
                    d = re.sub(r'\s+', ' ', d).strip()
                    if d: result["description"] = d[:2000]

                # Title
                title = _nd_str(["subject", "title", "name"], ad_data)
                if title: result["title"] = title

                # VIN (rare but present)
                vin_raw = _nd_str(["vin", "chassis", "chassinummer"], ad_data)
                if vin_raw and len(vin_raw) >= 10:
                    result["vin"] = re.sub(r"[^A-Z0-9]", "", vin_raw.upper())[:17]

                # Emission class, CO2
                ec = _nd_str(["emissionClass", "miljöklass", "euroklass"], ad_data)
                if ec: result["emission_class"] = ec
                co2 = _nd_str(["co2", "co2Emission", "koldioxidutsläpp"], ad_data)
                co2_m = re.search(r"(\d+)", co2 or "")
                if co2_m:
                    try: result["co2_emissions_g_km"] = int(co2_m.group(1))
                    except ValueError: pass

                # Interior color
                ic_raw = _nd_str(["interiorColor", "inredning", "invändig färg"], ad_data)
                if ic_raw:
                    ic, ic_ua = normalize_color(ic_raw)
                    if ic:
                        result["interior_color"] = ic
                        result["interior_color_ua"] = ic_ua

                # Cylinders, gears, weight
                cyl_raw = _nd_str(["cylinders", "cylindrar"], ad_data)
                cyl_m = re.search(r"(\d+)", cyl_raw or "")
                if cyl_m:
                    try: result["cylinders"] = int(cyl_m.group(1))
                    except ValueError: pass
                g_raw = _nd_str(["gears", "växlar"], ad_data)
                g_m = re.search(r"(\d+)", g_raw or "")
                if g_m:
                    try: result["gears"] = int(g_m.group(1))
                    except ValueError: pass
                w_raw = _nd_str(["weight", "tjänstevikt", "vikt"], ad_data)
                w_m = re.search(r"(\d+)", (w_raw or "").replace(" ", ""))
                if w_m:
                    try: result["weight_kg"] = int(w_m.group(1))
                    except ValueError: pass

            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"[blocket:detail:nextdata] Parse failed for {url}: {e}")
                # Continue to ld+json fallback below (don't return early)

        # ── ld+json — structured data ─────────────────────────────────────
        for ld_match in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
            try:
                ld = json.loads(ld_match.group(1))
                if ld.get("@type") == "Product" or ld.get("@type") == "Car":
                    # Price
                    offers = ld.get("offers", {})
                    if isinstance(offers, dict):
                        price_val = offers.get("price")
                        if price_val and not result["price_sek"]:
                            try:
                                result["price_sek"] = float(price_val)
                            except (ValueError, TypeError):
                                pass

                    # Images (only if __NEXT_DATA__ didn't provide any).
                    # Normalize to /dynamic/1280w/ — 1600w is rejected by Blocket CDN (404).
                    if not result["photos"]:
                        images = ld.get("image", [])
                        if isinstance(images, list):
                            for img in images:
                                src = img.get("contentUrl") or img.get("url") or (img if isinstance(img, str) else "")
                                if src:
                                    src = re.sub(r'/dynamic/[^/]+/', '/dynamic/1280w/', src)
                                    result["photos"].append(src)

                    # Vehicle properties from ld+json (only fill missing fields)
                    if not result["make"]:
                        brand_ld = ld.get("brand")
                        result["make"] = (brand_ld.get("name") if isinstance(brand_ld, dict) else str(brand_ld or "")) or None
                    if not result["model"]:
                        model_ld = ld.get("model") or ld.get("name", "")
                        result["model"] = model_ld.get("name") if isinstance(model_ld, dict) else str(model_ld or "")

                    # Additional properties
                    props = ld.get("additionalProperty", [])
                    for prop in props:
                        if not isinstance(prop, dict):
                            continue
                        pname = (prop.get("name") or "").lower()
                        pval = prop.get("value", "")
                        if pname in ("miltal", "mileage", "körsträcka") and not result["mileage"]:
                            km = clean_int(str(pval))
                            if km:
                                result["mileage"] = km * 10 if km < 100000 else km
                        elif pname in ("årsmodell", "year", "modellår") and not result["year"]:
                            ym = re.search(r'(20\d{2})', str(pval))
                            if ym:
                                result["year"] = int(ym.group(1))
                        elif pname in ("drivmedel", "fuel", "bränsle") and not result["fuel"]:
                            result["fuel"] = normalize_fuel(str(pval))
                        elif pname in ("växellåda", "gearbox", "transmission") and not result["transmission"]:
                            result["transmission"] = normalize_transmission(str(pval))
                        elif pname in ("hästkrafter", "horsepower", "effekt") and not result["horsepower"]:
                            hp = clean_int(str(pval))
                            if hp and hp < 2000:
                                result["horsepower"] = hp
                        elif pname in ("karosseri", "body", "fordonstyp") and not result["body_type"]:
                            result["body_type"], result["body_type_ua"] = normalize_body_type(str(pval))
                        elif pname in ("färg", "color") and not result["color"]:
                            result["color"], result["color_ua"] = normalize_color(str(pval))
                        elif pname in ("dörrar", "doors") and not result["doors"]:
                            d = clean_int(str(pval))
                            if d:
                                result["doors"] = d
                        elif pname in ("säten", "seats") and not result["seats"]:
                            s = clean_int(str(pval))
                            if s:
                                result["seats"] = s
                        elif pname in ("drivning", "drive") and not result["drive"]:
                            result["drive"] = normalize_drive(str(pval))
                    break  # Found our Product/Car
            except (json.JSONDecodeError, Exception):
                continue

        # ── Photos fallback: UUID method ──────────────────────────────────
        if not result["photos"]:
            seen_photos: set = set()
            matches = re.findall(
                rf"https://images\.blocketcdn\.se/dynamic/[^/]+/item/{item_id}/([0-9a-f-]{{36}})",
                html
            )
            for uuid in dict.fromkeys(matches):
                p = f"https://images.blocketcdn.se/dynamic/1280w/item/{item_id}/{uuid}"
                if p not in seen_photos:
                    seen_photos.add(p)
                    result["photos"].append(p)

        result["photos"] = result["photos"][:20]

        # ── Features from description/specs ───────────────────────────────
        # Blocket often has equipment in a list in the description
        spec_section = re.search(r'(?:utrustning|equipment|egenskaper).*?<ul[^>]*>(.*?)</ul>', html, re.DOTALL | re.IGNORECASE)
        if spec_section:
            items = re.findall(r'<li[^>]*>(.*?)</li>', spec_section.group(1), re.DOTALL)
            for item in items:
                name = decode_html(re.sub(r'<[^>]+>', '', item)).strip()
                if name and len(name) > 2:
                    result["all_features"].append(name)

    except Exception as e:
        logger.warning(f"[blocket:detail] {url}: {e}")

    return result


async def _parse_blocket_card(card) -> Optional[dict]:
    """Витягує базові дані з картки в списку Blocket."""
    try:
        text = await card.inner_text()
        if not text.strip() or len(text) < 20:
            return None

        # URL
        href = ""
        for lnk in await card.query_selector_all("a"):
            h = await lnk.get_attribute("href") or ""
            if h and len(h) > 10 and not h.startswith("#"):
                href = h
                break
        if not href:
            return None
        url = href if href.startswith("http") else f"https://www.blocket.se{href}"
        if not re.search(r'[\d]{5,}|[0-9a-f]{8}', url):
            return None

        # Title
        title_el = await card.query_selector("h2, h3, [class*='title'], [class*='heading']")
        title = (await title_el.inner_text()).strip() if title_el else text.split("\n")[0].strip()

        words = title.split()
        if len(words) < 2 or not is_known_brand(words[0]):
            return None

        # Image
        img_el = await card.query_selector("img[src*='blocketcdn'], img[src*='images.']")
        if not img_el:
            img_el = await card.query_selector("img")
        image = await img_el.get_attribute("src") if img_el else None
        if image and (image.startswith("data:") or len(image) < 10):
            image = None
        if image:
            # Blocket CDN: only 1280w/1600w/default are valid presets — 1024w
            # returns 404 with `X-Reason: Invalid Rule`. Use 1280w (smallest valid).
            image = re.sub(r'/\d+w/', '/1280w/', image)

        # Basic data from card text
        year_m = re.search(r'\b(20(?:0[5-9]|1[0-9]|2[0-6]))\b', text)
        year = int(year_m.group(1)) if year_m else None

        mil_m = re.search(r'([\d\s]+)\s*mil\b', text, re.IGNORECASE)
        if mil_m:
            mileage = int(float(re.sub(r'\s', '', mil_m.group(1))) * 10)
        else:
            km_m = re.search(r'([\d\s]+)\s*km\b', text, re.IGNORECASE)
            mileage = clean_int(km_m.group(1)) if km_m else None

        price_m = re.search(r'([\d\s]+)\s*kr\b', text, re.IGNORECASE)
        price_sek = float(re.sub(r'\s', '', price_m.group(1))) if price_m else None

        return {
            "url": url,
            "title": title,
            "make": words[0],
            "model_words": words[1:],
            "year": year,
            "mileage": mileage,
            "price_sek": price_sek,
            "image": image,
            "fuel": parse_fuel_from_text(text),
            "transmission": normalize_transmission(text),
            "horsepower": None,
        }

    except Exception as e:
        logger.debug(f"[blocket:card] {e}")
        return None


async def parse_blocket(
    context: BrowserContext,
    sek_to_eur_rate: float,
    brand: str = "",
    model: str = "",
    year_from: int = 2018,
    year_to: Optional[int] = None,
    max_results: int = 30,
    fuel: Optional[str] = None,
    transmission: Optional[str] = None,
    body_type: Optional[str] = None,
    price_to_eur: Optional[int] = None,
    vehicle_type: str = "Car",
) -> list[ParsedCar]:
    """
    Blocket: Playwright for list → HTTP for details (ld+json + photos).

    Blocket URL segment + category-ID per vehicle_type:
      Car → /fordon/bilar, cg=1020
      Motorcycle → /fordon/motorcyklar, cg=1040
      Truck/Van/Bus → /fordon/lastbilar_bussar, cg=1060
      Camper → /fordon/husbilar_husvagnar, cg=1080
    """
    _vt_map = {
        "Car": ("bilar", "1020"),
        "Motorcycle": ("motorcyklar", "1040"),
        "Truck": ("lastbilar_bussar", "1060"),
        "Van": ("lastbilar_bussar", "1060"),
        "Bus": ("lastbilar_bussar", "1060"),
        "Camper": ("husbilar_husvagnar", "1080"),
    }
    path_seg, cg = _vt_map.get(vehicle_type, ("bilar", "1020"))
    base = f"https://www.blocket.se/annonser/hela_sverige/fordon/{path_seg}"

    # Blocket packs additional filters into the URL-encoded `params=` blob —
    # multiple `key=value` pairs glued together with URL-encoded `&` (%26).
    # Adding year (mreg_min/mreg_max) tightens results; without it Blocket
    # returned 42 Honda Accords of 2003-2010 for a 2019-2022 query. Price
    # filter is in SEK on Blocket so we convert from EUR with the existing
    # rate.
    params_kv = ["item_condition=used"]
    if year_from:
        params_kv.append(f"mreg_min={year_from}")
    if year_to:
        params_kv.append(f"mreg_max={year_to}")
    if price_to_eur and sek_to_eur_rate > 0:
        price_to_sek = int(round(price_to_eur / sek_to_eur_rate))
        params_kv.append(f"price_max={price_to_sek}")
    # URL-encode `=` and `&` per Blocket convention so the whole stays in a
    # single `params=` query value.
    params_blob = "%26".join(p.replace("=", "%3D") for p in params_kv)

    if brand:
        q = f"{brand}+{model}".strip("+ ")
        url = f"{base}?sort=date&cg={cg}&q={q}&params={params_blob}"
    else:
        url = f"{base}?sort=date&cg={cg}&params={params_blob}"

    logger.info(f"[blocket] {url}")
    cards_data: list[dict] = []

    # ── Step 1: Playwright — list page ────────────────────────────────────
    page = await context.new_page()
    await page.route(
        "**/*.{png,jpg,jpeg,gif,webp,svg,css,woff,woff2,ttf,eot,mp4,mp3}",
        lambda r: r.abort(),
    )
    try:
        # domcontentloaded + wait_for_selector is faster than networkidle + fixed sleep.
        # We only need the article cards to be present, not every analytics pixel loaded.
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        # Race: wait for cards OR 1s ceiling (whichever first).
        try:
            await page.wait_for_selector("article, [data-testid*='ListItem'], a[href*='/annons/']", timeout=1500)
        except Exception:
            pass

        cards = await page.query_selector_all("article")
        if not cards:
            cards = await page.query_selector_all("[data-testid*='ListItem'], [class*='StyledArticle'], [class*='AdCard'], [class*='listing-card']")
        if not cards:
            cards = await page.query_selector_all("a[href*='/annons/']")
        logger.info(f"[blocket] Cards: {len(cards)}")

        for card in cards[:max_results * 2]:  # parse more, filter later
            data = await _parse_blocket_card(card)
            if data:
                if brand and brand.lower() not in data["make"].lower():
                    continue
                cards_data.append(data)
                if len(cards_data) >= max_results:
                    break

    except Exception as e:
        logger.error(f"[blocket] list error: {e}")
    finally:
        await page.close()

    logger.info(f"[blocket] Cards parsed: {len(cards_data)}")

    if not cards_data:
        return []

    # ── Step 2: HTTP — detail pages + photos ──────────────────────────────
    deals: list[ParsedCar] = []
    semaphore = asyncio.Semaphore(5)

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        async def fetch_one(card_data: dict, idx: int) -> Optional[ParsedCar]:
            async with semaphore:
                try:
                    detail_url = card_data["url"]
                    d = await _fetch_blocket_detail(detail_url, client)

                    make_raw = d.get("make") or card_data["make"]
                    make = normalize_make(make_raw)
                    model_raw = d.get("model") or " ".join(card_data["model_words"][:2])
                    model_name = normalize_model(model_raw, make)

                    year = d.get("year") or card_data.get("year")
                    mileage = d.get("mileage") or card_data.get("mileage")
                    price_sek = d.get("price_sek") or card_data.get("price_sek")
                    price_eur = round(price_sek * sek_to_eur_rate) if price_sek else None

                    photos = d.get("photos", [])
                    image = photos[0] if photos else card_data.get("image")
                    gallery = photos[1:] if len(photos) > 1 else []

                    fuel_val = d.get("fuel") or card_data.get("fuel")
                    trans_val = d.get("transmission") or card_data.get("transmission")
                    hp_val = d.get("horsepower") or card_data.get("horsepower")

                    raw_features = d.get("all_features", [])
                    feats = translate_and_categorize_features(raw_features)

                    car = ParsedCar(
                        source_site="blocket.se",
                        source_url=detail_url,
                        external_id=extract_uuid(detail_url) or detail_url.split("/")[-1],
                        make=make, model=model_name,
                        year=year, price_eur=price_eur,
                        price_original=price_sek, price_currency="SEK",
                        mileage_km=mileage,
                        fuel=fuel_val, fuel_ua=FUEL_UA.get(fuel_val or "", None),
                        transmission=trans_val, horsepower=hp_val,
                        drive=d.get("drive"),
                        body_type=d.get("body_type"), body_type_ua=d.get("body_type_ua"),
                        color=d.get("color"), color_ua=d.get("color_ua"),
                        doors=d.get("doors"), seats=d.get("seats"),
                        image=image, gallery=gallery,
                        safety_features=feats["safety"], comfort_features=feats["comfort"],
                        infotainment=feats["infotainment"], features_other=feats["other"],
                        country="Sweden",
                        previous_owners=d.get("previous_owners"),
                        accident_free=d.get("accident_free"),
                        service_history=d.get("service_history"),
                        has_damage=d.get("has_damage"),
                        inspection_valid_until=d.get("inspection_valid_until"),
                        # Extended spec (Blocket)
                        cylinders=d.get("cylinders"),
                        gears=d.get("gears"),
                        weight_kg=d.get("weight_kg"),
                        co2_emissions_g_km=d.get("co2_emissions_g_km"),
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
                        has_color=bool(car.color), has_drive=bool(car.drive),
                        has_body_type=bool(car.body_type),
                        previous_owners=car.previous_owners,
                        accident_free=car.accident_free,
                        service_history=car.service_history,
                        has_damage=car.has_damage,
                        premium_feature_count=premium,
                    )

                    logger.debug(
                        f"[blocket] [{idx+1}/{len(cards_data)}] {car.make} {car.model} "
                        f"| {price_eur}€ | photos={len(gallery)+(1 if image else 0)} "
                        f"| color={car.color} doors={car.doors}"
                    )
                    return car
                except Exception as e:
                    logger.warning(f"[blocket:fetch_one:{idx}] {e}")
                    return None

        tasks = [fetch_one(cd, i) for i, cd in enumerate(cards_data)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        deals = [d for d in results if isinstance(d, ParsedCar)]

    # Post-filters
    if fuel:
        expected = normalize_fuel(fuel)
        if expected:
            deals = [d for d in deals if not d.fuel or d.fuel == expected]
    if transmission:
        expected_t = normalize_transmission(transmission)
        if expected_t:
            deals = [d for d in deals if not d.transmission or d.transmission == expected_t]

    logger.info(f"[blocket] Done: {len(deals)} cars")
