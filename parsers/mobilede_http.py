# parsers/mobilede_http.py
# Mobile.de HTML scraper — works WITHOUT dealer API credentials.
#
# Mobile.de protects www.mobile.de with Akamai Bot Manager which does JS-challenge
# fingerprinting. Raw curl_cffi from a datacenter IP gets 403/challenge page (~2.5 KB).
# A residential/consumer IP typically passes the challenge (real users do).
#
# Strategy:
#   1. curl_cffi chrome136 TLS fingerprint + realistic headers
#   2. Optional proxy via MOBILEDE_PROXY_URL env (free: Windscribe DE 10GB/mo,
#      paid: IPRoyal/Oxylabs). Proxy unlocks scraping from Akamai-flagged IPs.
#   3. Parse __NEXT_DATA__ / __INITIAL_STATE__ from HTML — stable across releases.
#
# If both API creds and proxy are missing, returns [] silently.

import asyncio
import os
import re
import json
import random
from typing import Optional
from loguru import logger

try:
    from curl_cffi.requests import AsyncSession
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

BASE = "https://www.mobile.de"

# Mobile.de URL path per vehicle_type. The `scopeId` param also changes:
#   Car = "C", Motorcycle = "MB", Truck/Van/Bus = "C" with category filter.
# Note: motorrad.mobile.de is a separate subdomain; we route to the right path
# on www.mobile.de and set vc= (vehicleClass) + scopeId= appropriately.
_VC_MAP = {
    "Car": ("Car", "C"),
    "Motorcycle": ("Motorbike", "MB"),
    "Truck": ("SemiTrailerTruck", "TC"),
    "Van": ("VanUpTo7500", "TC"),
    "Bus": ("Coach", "TC"),
    "Camper": ("Caravan", "LP"),
}
PROXY_URL = os.getenv("MOBILEDE_PROXY_URL") or None

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Make → Mobile.de numeric makeId (from refdata API, see parser_mobilede.MOBILEDE_MAKES).
# These are the URL params: `?makeModelVariant1.makeId=3500` for BMW.
_MAKE_IDS: dict[str, int] = {
    "bmw": 3500, "audi": 1900, "mercedes-benz": 17200, "volkswagen": 25200,
    "volvo": 25100, "toyota": 24100, "honda": 10100, "mazda": 16800,
    "skoda": 22900, "ford": 9000, "opel": 19000, "peugeot": 19800,
    "renault": 20800, "hyundai": 11600, "kia": 13200, "nissan": 18700,
    "porsche": 20000, "tesla": 24400, "mini": 17800, "seat": 22500,
    "citroen": 5700, "mitsubishi": 17700, "subaru": 23500, "lexus": 15300,
    "jeep": 13000, "jaguar": 12800, "land rover": 14600, "dacia": 7300,
    "fiat": 8600, "suzuki": 23700, "alfa romeo": 1100, "cupra": 6900,
}


def build_url(brand: str = "", model_name: str = "", year_from: int = 2018,
              year_to: Optional[int] = None, price_max: Optional[int] = None,
              price_min: Optional[int] = None, max_mileage: int = 200000,
              page: int = 1, vehicle_type: str = "Car") -> str:
    """Build mobile.de search URL using the /fahrzeuge/search.html format.
    vc= + scopeId= together drive the category filter (Car/Motorbike/Truck/etc).
    """
    vc, scope = _VC_MAP.get(vehicle_type, ("Car", "C"))
    params = {
        "isSearchRequest": "true",
        "scopeId": scope,
        "vc": vc,
        "damageUnrepaired": "NO_DAMAGE_UNREPAIRED",
        "pageNumber": str(page),
    }
    make_id = _MAKE_IDS.get((brand or "").lower())
    if make_id:
        params["makeModelVariant1.makeId"] = str(make_id)
        if model_name:
            # Auto-convert: "3er" → "3ER", "A-Class" → "A CLASS"
            model_key = re.sub(r'[\s-]+', ' ', model_name.upper()).strip()
            params["makeModelVariant1.modelDescription"] = model_key
    if year_from:
        params["minFirstRegistrationDate"] = f"{year_from}-01-01"
    if year_to:
        params["maxFirstRegistrationDate"] = f"{year_to}-12-31"
    if price_max:
        params["maxPrice"] = str(price_max)
    if price_min:
        params["minPrice"] = str(price_min)
    if max_mileage:
        params["maxMileage"] = str(max_mileage)

    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{BASE}/fahrzeuge/search.html?{qs}"


def _extract_initial_state(html: str) -> Optional[dict]:
    """Extract window.__INITIAL_STATE__ JSON from Mobile.de SSR HTML."""
    idx = html.find("__INITIAL_STATE__")
    if idx < 0:
        return None
    eq = html.find("=", idx)
    start = html.find("{", eq)
    if start < 0:
        return None
    # Balanced-brace walk (strings with {} mess this up but SSR is usually clean).
    depth = 0
    end = start
    for i in range(start, min(start + 2_000_000, len(html))):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        return json.loads(html[start:end])
    except json.JSONDecodeError:
        return None


def _walk_for_ads(data) -> list[dict]:
    """Locate the list of ads inside __INITIAL_STATE__ — structure varies by page type."""
    found: list[dict] = []

    def visit(obj):
        if isinstance(obj, dict):
            # Common shapes: {"items": [...]}, {"ads": [...]}, {"results": {"items": [...]}}
            for key in ("items", "ads", "results", "data"):
                v = obj.get(key)
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    sample = v[0]
                    if any(k in sample for k in ("mobileAdId", "adId", "id", "detailPageUrl", "make")):
                        if any(k in sample for k in ("price", "priceRating", "make", "model")):
                            found.append(v)
                            return  # prefer first-found ad list
            for v in obj.values():
                visit(v)
        elif isinstance(obj, list):
            for item in obj[:5]:  # don't iterate massive lists
                visit(item)

    visit(data)
    return found[0] if found else []


def _parse_ad_dict(ad: dict) -> Optional[dict]:
    """Normalize a Mobile.de SSR ad dict into a flat shape."""
    ad_id = str(ad.get("mobileAdId") or ad.get("adId") or ad.get("id", ""))
    if not ad_id:
        return None

    url = ad.get("detailPageUrl") or ad.get("url") or ""
    if url and not url.startswith("http"):
        url = f"{BASE}{url}"
    if not url:
        url = f"{BASE}/auto-inserat/{ad_id}.html"

    price = None
    price_obj = ad.get("price")
    if isinstance(price_obj, dict):
        for k in ("gross", "grossAmount", "amount", "value"):
            if price_obj.get(k) is not None:
                try:
                    price = float(price_obj[k])
                    break
                except (ValueError, TypeError):
                    pass
        if not price and price_obj.get("formatted"):
            digits = re.sub(r"[^\d]", "", str(price_obj["formatted"]))
            if digits:
                price = float(digits)
    elif isinstance(price_obj, (int, float)):
        price = float(price_obj)

    # Flatten attributes — Mobile.de puts year/mileage/fuel/power in `attr` dict
    attr = ad.get("attr") or {}
    if not isinstance(attr, dict):
        attr = {}
    fmt = ad.get("formattedAttributes") or []

    mileage = None
    for v in (attr.get("mileage"), attr.get("km"), ad.get("mileage")):
        if v:
            try:
                mileage = int(re.sub(r"[^\d]", "", str(v)))
                break
            except (ValueError, TypeError):
                pass

    year = None
    reg = attr.get("firstRegistration") or attr.get("year") or ad.get("firstRegistration", "")
    if reg:
        m = re.search(r"(20\d{2}|19\d{2})", str(reg))
        if m:
            year = int(m.group(1))

    hp = None
    power = attr.get("power") or attr.get("hp") or ad.get("power")
    if power:
        m = re.search(r"(\d+)\s*(?:hp|ps|kw)?", str(power), re.IGNORECASE)
        if m:
            val = int(m.group(1))
            # heuristic: values <300 with "kw" in text are kW → *1.341
            if "kw" in str(power).lower() and val < 300:
                hp = int(val * 1.341)
            else:
                hp = val

    fuel = attr.get("fuel") or ad.get("fuel") or ""
    transmission = attr.get("transmission") or attr.get("gearbox") or ""

    # Images — shape varies: [{s, m, l, xl}], [url], or {sizes: {...}}
    image_list = ad.get("image") or ad.get("images") or []
    if isinstance(image_list, dict):
        image_list = [image_list]
    images: list[str] = []
    for img in image_list if isinstance(image_list, list) else []:
        if isinstance(img, str) and img:
            images.append(img)
        elif isinstance(img, dict):
            src = (img.get("xxxl") or img.get("xxl") or img.get("xl")
                   or img.get("l") or img.get("m") or img.get("src") or img.get("url") or "")
            if src:
                images.append(src)

    return {
        "id": ad_id,
        "url": url,
        "make": str(ad.get("make", "")),
        "model": str(ad.get("model", "") or ad.get("modelDescription", "")),
        "price": price,
        "year": year,
        "mileage": mileage,
        "fuel": str(fuel) if fuel else None,
        "transmission": str(transmission) if transmission else None,
        "horsepower": hp,
        "images": images,
        "country": "Germany",
    }


async def search_html(
    brand: str = "",
    model_name: str = "",
    year_from: int = 2018,
    year_to: Optional[int] = None,
    price_max: Optional[int] = None,
    price_min: Optional[int] = None,
    max_mileage: int = 200000,
    max_results: int = 20,
    vehicle_type: str = "Car",
) -> list[dict]:
    """Scrape Mobile.de via HTML. Returns flat dicts (same shape as as24_http.search)."""
    if not _HAS_CURL_CFFI:
        logger.warning("[mobilede:http] curl_cffi not installed — cannot scrape")
        return []

    url = build_url(brand=brand, model_name=model_name, year_from=year_from,
                    year_to=year_to, price_max=price_max, price_min=price_min,
                    max_mileage=max_mileage, page=1, vehicle_type=vehicle_type)

    session_kwargs: dict = {"impersonate": "chrome136", "headers": _HEADERS, "timeout": 20}
    if PROXY_URL:
        # curl_cffi supports the proxies= arg (dict like requests).
        session_kwargs["proxies"] = {"http": PROXY_URL, "https": PROXY_URL}
        logger.info(f"[mobilede:http] using proxy {PROXY_URL[:25]}...")

    from .rate_limiter import acquire as _rl

    try:
        async with AsyncSession(**session_kwargs) as s:
            await _rl("mobile.de")
            # One retry on 403/429 with jitter (Akamai transient challenge).
            resp = await s.get(url, allow_redirects=True)
            if resp.status_code in (403, 429) or (resp.status_code == 200 and len(resp.text) < 10_000):
                await asyncio.sleep(random.uniform(2.0, 4.0))
                resp = await s.get(url, allow_redirects=True)

            # Without a residential proxy, Akamai reliably blocks us. Don't spam
            # warnings — log at DEBUG once, let /metrics reflect the "blocked" kind.
            if resp.status_code != 200:
                logger.debug(f"[mobilede:http] HTTP {resp.status_code} (Akamai block — expected without MOBILEDE_PROXY_URL)")
                return []

            if len(resp.text) < 10_000:
                logger.debug(f"[mobilede:http] challenge page ({len(resp.text)}B) — set MOBILEDE_PROXY_URL to unblock")
                return []

            data = _extract_initial_state(resp.text)
            if not data:
                logger.warning("[mobilede:http] __INITIAL_STATE__ not found in HTML")
                return []

            ads = _walk_for_ads(data)
            if not ads:
                logger.info("[mobilede:http] no ads in __INITIAL_STATE__ (homepage or empty result)")
                return []

            parsed = []
            for a in ads[:max_results]:
                p = _parse_ad_dict(a)
                if p and p.get("make"):
                    parsed.append(p)

            logger.info(f"[mobilede:http] parsed {len(parsed)} ads from HTML")
            return parsed
    except Exception as e:
        logger.warning(f"[mobilede:http] fetch failed: {e.__class__.__name__}: {e}")
        return []
