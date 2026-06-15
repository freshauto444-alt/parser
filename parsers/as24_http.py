# parsers/as24_http.py
# AutoScout24 HTTP-only parser — NO Playwright needed.
#
# Discovered via HAR analysis (April 2026):
#   - AS24 is Next.js with __NEXT_DATA__ containing full listing data (20 per page)
#   - BuildId format: as24-search-funnel_main-YYYYMMDDHHMMSS
#   - Asset prefix: /assets/as24-search-funnel
#   - GraphQL auth: Basic as24-search-funnel:<password> (hardcoded in frontend)
#   - 1 HTTP request = 20 cars with: make, model, price, mileage, fuel, HP, images, location
#
# Two approaches (primary + fallback):
#   1. Direct HTML fetch + __NEXT_DATA__ extraction (most reliable)
#   2. _next/data JSON endpoint (faster but needs buildId management)

import asyncio
import json
import re
import time
import random
from typing import Optional
from loguru import logger

try:
    from curl_cffi.requests import AsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    logger.warning("[as24] curl_cffi not installed — using httpx fallback (may get blocked by Akamai)")

if not HAS_CURL_CFFI:
    import httpx

BASE_URL = "https://www.autoscout24.com"

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9,de;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# ── AS24 URL constants and builders (canonical location) ──────────────────────

AS24_FUEL = {"Petrol": "B", "Diesel": "D", "Electric": "E", "Hybrid": "H", "LPG": "L", "CNG": "M"}
AS24_TRANS = {"Automatic": "A", "Manual": "M"}
# Codes verified empirically (May 2026):
#   body=1 → Compact (Golf, Panda, 208)
#   body=2 → Convertible/Cabrio
#   body=3 → Coupe
#   body=4 → SUV / Off-Road
#   body=5 → Estate / Station Wagon
#   body=6 → Sedan
#   body=7 → Camper Van
#   body=12 → Minivan / MPV
#   body=13 → Transporter / Truck
AS24_BODY = {"Sedan": "6", "Hatchback": "1", "Estate": "5", "Coupe": "3", "Convertible": "2", "SUV": "4", "Van": "12", "Pickup": "13"}
AS24_MODEL_SLUG = {
    ("bmw", "1 series"): "1er", ("bmw", "2 series"): "2er",
    ("bmw", "3 series"): "3er", ("bmw", "3er"): "3er",
    ("bmw", "4 series"): "4er", ("bmw", "5 series"): "5er", ("bmw", "5er"): "5er",
    ("bmw", "6 series"): "6er", ("bmw", "7 series"): "7er", ("bmw", "8 series"): "8er",
    ("bmw", "x3"): "x3", ("bmw", "x5"): "x5", ("bmw", "x1"): "x1",
    ("mercedes-benz", "a-class"): "a-klasse", ("mercedes-benz", "b-class"): "b-klasse",
    ("mercedes-benz", "c-class"): "c-klasse", ("mercedes-benz", "c-klasse"): "c-klasse",
    ("mercedes-benz", "e-class"): "e-klasse", ("mercedes-benz", "e-klasse"): "e-klasse",
    ("mercedes-benz", "s-class"): "s-klasse", ("mercedes-benz", "glc"): "glc", ("mercedes-benz", "gle"): "gle",
    ("volkswagen", "golf"): "golf", ("volkswagen", "passat"): "passat", ("volkswagen", "tiguan"): "tiguan",
    ("volvo", "v60"): "v60", ("volvo", "xc60"): "xc60", ("volvo", "xc40"): "xc40",
    ("audi", "a4"): "a4", ("audi", "a6"): "a6", ("audi", "q5"): "q5",
    ("toyota", "rav4"): "rav4", ("toyota", "corolla"): "corolla",
    ("skoda", "octavia"): "octavia", ("skoda", "superb"): "superb",
    ("porsche", "cayenne"): "cayenne", ("porsche", "macan"): "macan",

    # Performance trims (AMG / M / RS) → route to base-class URL. AS24 has no
    # /mercedes-benz/e-63 page, so a direct slug returns 404 and we miss the
    # biggest source. Sending to /e-klasse returns all E-Class cars; the
    # client filter (filterCarsClientSide) narrows back to the specific trim.
    # Mercedes-AMG
    ("mercedes-benz", "e 63"): "e-klasse", ("mercedes-benz", "e63"): "e-klasse",
    ("mercedes-benz", "e 53"): "e-klasse", ("mercedes-benz", "e 43"): "e-klasse",
    ("mercedes-benz", "c 63"): "c-klasse", ("mercedes-benz", "c63"): "c-klasse",
    ("mercedes-benz", "c 43"): "c-klasse",
    ("mercedes-benz", "s 63"): "s-klasse", ("mercedes-benz", "s 65"): "s-klasse",
    ("mercedes-benz", "a 45"): "a-klasse", ("mercedes-benz", "a 35"): "a-klasse",
    ("mercedes-benz", "cla 45"): "cla", ("mercedes-benz", "cla 35"): "cla",
    ("mercedes-benz", "gla 45"): "gla", ("mercedes-benz", "gla 35"): "gla",
    ("mercedes-benz", "glc 63"): "glc", ("mercedes-benz", "glc 43"): "glc",
    ("mercedes-benz", "gle 63"): "gle", ("mercedes-benz", "gle 53"): "gle",
    ("mercedes-benz", "g 63"): "g-klasse", ("mercedes-benz", "g 65"): "g-klasse",
    ("mercedes-benz", "cls 53"): "cls", ("mercedes-benz", "cls 63"): "cls",
    # BMW M
    ("bmw", "m2"): "2er", ("bmw", "m3"): "3er", ("bmw", "m4"): "4er",
    ("bmw", "m5"): "5er", ("bmw", "m6"): "6er", ("bmw", "m8"): "8er",
    ("bmw", "x3 m"): "x3", ("bmw", "x4 m"): "x4",
    ("bmw", "x5 m"): "x5", ("bmw", "x6 m"): "x6",
    # Audi RS / S
    ("audi", "rs3"): "a3", ("audi", "rs4"): "a4", ("audi", "rs5"): "a5",
    ("audi", "rs6"): "a6", ("audi", "rs7"): "a7",
    ("audi", "rs q3"): "q3", ("audi", "rs q8"): "q8",
    ("audi", "s3"): "a3", ("audi", "s4"): "a4", ("audi", "s5"): "a5",
    ("audi", "s6"): "a6", ("audi", "s7"): "a7", ("audi", "s8"): "a8",
    # VW Golf R / GTI map to base "golf"
    ("volkswagen", "golf r"): "golf", ("volkswagen", "golf gti"): "golf",
}

# AS24 uses a `cat=ma{brand}gr{group}mt{trim}` query param to filter to a
# specific performance variant directly (no broader-class fallback needed).
# Discovered by inspecting AS24's faceted-search URLs. When a key matches
# here, we hit /lst/{brand}?cat=… instead of /lst/{brand}/{slug} and the
# upstream returns only the requested trim — no client-side narrowing
# required and source counts jump from a handful to hundreds.
#
# To add a variant: open AS24 search UI, pick the brand + AMG/M/RS trim,
# copy the `cat=` value from the URL bar.
AS24_MODEL_CAT = {
    # Mercedes-AMG E63 (all generations) — 248 results across DE/AT/BE/ES/FR/IT/LU/NL.
    # mt467 = AMG 4.0 V8 biturbo motortype_id; gr100061 = E-Klasse modelGroup;
    # ma47 = Mercedes-Benz makeId.
    ("mercedes-benz", "e 63"): "ma47gr100061mt467",
    ("mercedes-benz", "e63"): "ma47gr100061mt467",

    # Kia Ceed group — pilot for non-AMG cat lookups. /lst/kia/ceed (path-based)
    # was returning only 169 cars; cat=ma39gr200738 returns 939 (includes Ceed,
    # Ceed SW, all gens). ma39 = Kia makeId, gr200738 = Ceed modelGroup value
    # from taxonomy.modelGroups. No mt suffix = all motor variants.
    ("kia", "ceed"): "ma39gr200738",

    # Mercedes GL-family SUVs — the bare path slugs (/mercedes-benz/gls etc.)
    # return 404 on AS24, so these MUST go through the cat= facet. They were
    # missing from the harvested Supabase taxonomy, so resolve_cat() returned
    # None and the code fell back to the 404 slug → 0 results (user reported
    # "GLS found only on Bytbil, nothing from AS24"). Group ids verified live
    # against autoscout24 taxonomy.modelGroups[47]. ma47 = Mercedes-Benz makeId.
    ("mercedes-benz", "gls"): "ma47gr100122",
    ("mercedes-benz", "gls 63"): "ma47gr100122",
    ("mercedes-benz", "gls 350"): "ma47gr100122",
    ("mercedes-benz", "gls 400"): "ma47gr100122",
    ("mercedes-benz", "glb"): "ma47gr100149",
    ("mercedes-benz", "glk"): "ma47gr100083",
    ("mercedes-benz", "glk-klasse"): "ma47gr100083",
    ("mercedes-benz", "gl"): "ma47gr100063",
    ("mercedes-benz", "gl-klasse"): "ma47gr100063",
}


def build_as24_url(params: dict, page_num: int = 1, prefer_slug: bool = False) -> str:
    """Build AutoScout24 search URL using path-based format.

    Supports vehicle_type — AutoScout24 has separate path prefixes per type:
      - Car: /lst/
      - Motorcycle: /lst/motorrad/
      - Truck: /lst/ubernutzfahrzeug/   (commercial / LKW)
      - Camper: /lst/wohnmobil/
    Brand slugs stay the same (bmw, harley-davidson, etc).
    """
    # Slugify brand for AS24 path: "Aston Martin" → "aston-martin",
    # "Land Rover" → "land-rover", "Alfa Romeo" → "alfa-romeo". Mercedes-Benz
    # and Rolls-Royce already carry a dash, so this is a no-op for them.
    # Without slugification, the brand goes into the URL path with a literal
    # space and AS24 silently strips it, returning unfiltered results.
    brand_raw = (params.get("brand") or "").lower()
    brand = re.sub(r"\s+", "-", brand_raw).strip("-")
    model = (params.get("model") or "").lower()
    vtype = (params.get("vehicle_type") or "Car")
    # Path prefix per vehicle_type
    vtype_path = {
        "Motorcycle": "/lst/motorrad",
        "Truck": "/lst/ubernutzfahrzeug",
        "Van": "/lst/ubernutzfahrzeug",
        "Bus": "/lst/ubernutzfahrzeug",
        "Camper": "/lst/wohnmobil",
    }.get(vtype, "/lst")

    # Resolution priority for the cat= facet code (best → worst):
    #   1. Live taxonomy lookup in Supabase (as24_taxonomy.resolve_cat).
    #      Covers 287 brands × 2486 groups × 1878 motors harvested from
    #      AS24's __NEXT_DATA__. cat=ma+gr (and +mt for Mercedes AMG) gives
    #      5× more results than slug paths on average.
    #   2. AS24_MODEL_CAT — legacy hand-curated entries (E63, Ceed pilot).
    #   3. AS24_MODEL_SLUG → path-based URL, the original fallback.
    # We try each in order and emit the cat-URL as soon as we get a hit.
    from .as24_taxonomy import resolve_cat as _resolve_cat_db
    # Also try a "-class"/"-klasse"-stripped model name against the hardcoded cat
    # map, so "GLS-Class"/"GLS-Klasse" (how the chat sometimes names it) resolves
    # to the same cat as bare "gls" instead of falling through to the 404 slug.
    model_base = re.sub(r'[\s-]?(class|klasse)$', '', model).strip(' -')
    model_cat = None if prefer_slug else (
        _resolve_cat_db(brand, model)
        or AS24_MODEL_CAT.get((brand, model))
        or (AS24_MODEL_CAT.get((brand, model_base)) if model_base != model else None)
    )
    model_slug = AS24_MODEL_SLUG.get((brand, model)) or re.sub(r'\s+', '-', model).strip('-')

    if brand and model_cat:
        url = f"{BASE_URL}{vtype_path}/{brand}?cat={model_cat}&sort=standard&desc=0&ustate=N%2CU&page={page_num}"
    elif brand and model_slug:
        url = f"{BASE_URL}{vtype_path}/{brand}/{model_slug}?sort=standard&desc=0&ustate=N%2CU&page={page_num}"
    elif brand:
        url = f"{BASE_URL}{vtype_path}/{brand}?sort=standard&desc=0&ustate=N%2CU&page={page_num}"
    else:
        url = f"{BASE_URL}{vtype_path}?sort=standard&desc=0&ustate=N%2CU&page={page_num}"

    if params.get("year_from"):
        url += f"&fregfrom={params['year_from']}"
    if params.get("year_to"):
        url += f"&fregto={params['year_to']}"
    if params.get("price_from"):
        url += f"&pricefrom={params['price_from']}"
    if params.get("price_to"):
        url += f"&priceto={params['price_to']}"
    fuel_raw = params.get("fuel") or ""
    for f in fuel_raw.split(","):
        fc = AS24_FUEL.get(f.strip())
        if fc: url += f"&fuel={fc}"
    tc = AS24_TRANS.get(params.get("transmission") or "")
    if tc: url += f"&gear={tc}"
    body_raw = params.get("body_type") or ""
    for b in body_raw.split(","):
        bc = AS24_BODY.get(b.strip())
        if bc: url += f"&body={bc}"
    return url


_build_as24_url = build_as24_url  # alias for internal use


def _extract_next_data(html: str) -> Optional[dict]:
    """Extract and parse __NEXT_DATA__ JSON from HTML."""
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"[as24] __NEXT_DATA__ parse error: {e}")
        return None


def parse_listing_from_nextdata(listing: dict) -> dict:
    """
    Parse a single listing from AS24 __NEXT_DATA__ pageProps.listings[].
    Returns a flat dict ready for ParsedCar construction.

    Based on real HAR data structure (April 2026):
    - listing.vehicle: make, model, modelGroup, variant, fuel, transmission, mileageInKm, engineDisplacementInCCM
    - listing.vehicleDetails[]: {data, iconName, ariaLabel} — Mileage, Gear, First registration, Fuel type, Power
    - listing.price: {priceFormatted}
    - listing.location: {countryCode, zip, city}
    - listing.images[]: photo URLs
    - listing.url: detail page path
    - listing.seller: dealer info
    """
    vehicle = listing.get("vehicle", {})
    location = listing.get("location", {})
    price_obj = listing.get("price", {})

    # Parse vehicleDetails for structured data (more reliable than vehicle.* fields)
    vd_map = {}
    for vd in listing.get("vehicleDetails", []):
        label = (vd.get("ariaLabel") or "").lower()
        data = vd.get("data") or ""
        vd_map[label] = data

    # Make & Model
    make = vehicle.get("make", "")
    model = vehicle.get("model", "")
    model_group = vehicle.get("modelGroup", "")

    # Price — extract number from "€ 32,900" or "€ 32.900"
    price_str = price_obj.get("priceFormatted", "")
    price = None
    if price_str:
        digits = re.sub(r'[^\d]', '', price_str)
        if digits:
            price = int(digits)

    # Mileage — from vehicleDetails "29,840 km" or vehicle.mileageInKm
    mileage = None
    mileage_str = vd_map.get("mileage") or vehicle.get("mileageInKm") or ""
    if mileage_str:
        digits = re.sub(r'[^\d]', '', str(mileage_str))
        if digits:
            mileage = int(digits)

    # Year — from vehicleDetails "10/2022" (First registration)
    year = None
    reg_str = vd_map.get("first registration", "")
    if reg_str:
        yr_match = re.search(r'(\d{4})', reg_str)
        if yr_match:
            year = int(yr_match.group(1))

    # Fuel
    fuel = vd_map.get("fuel type") or vehicle.get("fuel") or None

    # Transmission
    transmission = vd_map.get("gear") or vehicle.get("transmission") or None

    # Power — "135 kW (184 hp)"
    hp = None
    power_str = vd_map.get("power", "")
    hp_match = re.search(r'(\d+)\s*hp', power_str)
    if hp_match:
        hp = int(hp_match.group(1))
    elif power_str:
        kw_match = re.search(r'(\d+)\s*kW', power_str)
        if kw_match:
            hp = int(int(kw_match.group(1)) * 1.341)

    # Engine displacement
    engine = None
    engine_cc = None
    disp_str = vehicle.get("engineDisplacementInCCM", "")
    if disp_str:
        disp_digits = re.sub(r'[^\d]', '', str(disp_str))
        if disp_digits:
            cc = int(disp_digits)
            if 600 <= cc <= 8500:  # sane car/SUV range; guards garbage values
                engine_cc = cc
            liters = round(cc / 1000, 1)
            fuel_short = fuel or ""
            engine = f"{liters} {fuel_short}".strip() if fuel_short else f"{liters}L"

    # Country — use canonical map from base.py
    from .base import COUNTRY_CODE_MAP
    country_code = location.get("countryCode", "")
    country = COUNTRY_CODE_MAP.get(country_code.upper(), "Europe") if country_code else "Europe"

    # Images — upgrade to higher resolution
    images = []
    for img_url in listing.get("images", []):
        if isinstance(img_url, str) and img_url:
            # Replace /250x188.webp with /800x600.webp for better quality
            hi_res = re.sub(r'/\d+x\d+\.\w+$', '/800x600.webp', img_url)
            images.append(hi_res)

    # Detail URL
    detail_url = listing.get("url", "")
    if detail_url and not detail_url.startswith("http"):
        detail_url = f"{BASE_URL}{detail_url}"

    # Listing ID
    listing_id = listing.get("id", "")

    return {
        "id": listing_id,
        "make": make,
        "model": model,
        "model_group": model_group,
        "variant": vehicle.get("variant", ""),
        "subtitle": vehicle.get("subtitle", ""),
        "model_version": vehicle.get("modelVersionInput", ""),
        "year": year,
        "price": price,
        "mileage": mileage,
        "fuel": fuel,
        "transmission": transmission,
        "horsepower": hp,
        "engine": engine,
        "engine_cc": engine_cc,
        "country": country,
        "city": location.get("city", ""),
        "images": images,
        "url": detail_url,
        "seller_type": listing.get("seller", {}).get("type", ""),
    }


async def search(
    params: dict,
    max_results: int = 20,
    sort: str = "standard",
) -> tuple[list[dict], int]:
    """
    Search AutoScout24 via HTTP — extracts __NEXT_DATA__ from HTML.
    Returns (listings, total_count).

    1 HTTP request = 20 listings with full data.
    For 40 results = 2 requests. For 60 = 3 requests.
    """
    # Build URL using existing function
    url = _build_as24_url(params, page_num=1)

    # Override sort if needed (sort=age&desc=1 = newest first)
    if sort == "newest":
        url = re.sub(r'sort=\w+', 'sort=age', url)
        url = re.sub(r'desc=\d', 'desc=1', url)
    elif sort == "price_asc":
        url = re.sub(r'sort=\w+', 'sort=price', url)
        url = re.sub(r'desc=\d', 'desc=0', url)
    elif sort == "mileage_asc":
        url = re.sub(r'sort=\w+', 'sort=km', url)
        url = re.sub(r'desc=\d', 'desc=0', url)

    # Create HTTP client.
    # chrome136 = latest supported fingerprint in curl_cffi 0.13 (2026 Chrome stable).
    # AS24 is behind Akamai Bot Manager — JA3+JA4+H2 SETTINGS must all match real Chrome.
    if HAS_CURL_CFFI:
        client = AsyncSession(impersonate="chrome136", headers=_HEADERS, timeout=20)
    else:
        client = httpx.AsyncClient(headers={**_HEADERS, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"}, timeout=20, follow_redirects=True)

    try:
        all_listings = []
        total_count = 0
        pages_to_fetch = max(1, (max_results + 19) // 20)
        pages_to_fetch = min(pages_to_fetch, 5)

        # ── Page 1: fetch immediately to get total_count ──────────────────
        page1_url = re.sub(r'page=\d+', 'page=1', url) if 'page=' in url else url + '&page=1'
        logger.debug(f"[as24] Fetching page 1...")

        from .rate_limiter import acquire as rate_acquire
        await rate_acquire("autoscout24.com")

        # One retry on 403/429/5xx — Akamai sometimes does a one-off challenge on cold IP.
        resp = await client.get(page1_url)
        if resp.status_code in (403, 429, 500, 502, 503) or (resp.status_code == 200 and "__NEXT_DATA__" not in resp.text):
            await asyncio.sleep(random.uniform(1.5, 3.0))
            resp = await client.get(page1_url)

        # Record classified outcome for observability
        try:
            from .metrics import classify_error, record
            if resp.status_code != 200:
                record("autoscout24", classify_error(http_status=resp.status_code),
                       0, error=f"HTTP {resp.status_code}")
            elif "__NEXT_DATA__" not in resp.text:
                record("autoscout24", "blocked", 0,
                       error=f"challenge page {len(resp.text)}B")
        except Exception:
            pass  # metrics must never break scrapes

        if resp.status_code != 200:
            logger.warning(f"[as24] Page 1 HTTP {resp.status_code}" + (" (challenge?)" if resp.status_code in (403, 429) else ""))
            return [], 0

        nd = _extract_next_data(resp.text)
        if not nd:
            logger.warning(f"[as24] No __NEXT_DATA__ on page 1 — HTML {len(resp.text)}B (likely bot challenge)")
            return [], 0

        pp = nd.get("props", {}).get("pageProps", {})
        total_count = pp.get("numberOfResults", 0)
        for raw in pp.get("listings", []):
            parsed = parse_listing_from_nextdata(raw)
            if parsed.get("make"):
                all_listings.append(parsed)

        logger.info(f"[as24] Page 1: {len(all_listings)} listings (total: {total_count})")

        # ── cat→slug self-heal ────────────────────────────────────────────
        # If the taxonomy-resolved cat= URL returned an empty page but the model
        # has a working path slug (e.g. /audi/sq5 → 19 cars), the harvested cat
        # was stale/wrong. Retry once via the slug before giving up — fixes the
        # recurring "0 from AutoScout24 even though the cars exist" reports.
        if not all_listings and "cat=" in url and (params.get("model") or ""):
            slug_url = _build_as24_url(params, page_num=1, prefer_slug=True)
            if slug_url != url:
                logger.info(f"[as24] cat= returned 0 — retry via slug: {slug_url.split('autoscout24.com')[-1].split('?')[0]}")
                if sort == "newest":
                    slug_url = re.sub(r'sort=\w+', 'sort=age', re.sub(r'desc=\d', 'desc=1', slug_url))
                await rate_acquire("autoscout24.com")
                r2 = await client.get(slug_url)
                if r2.status_code == 200 and "__NEXT_DATA__" in r2.text:
                    nd2 = _extract_next_data(r2.text)
                    pp2 = (nd2 or {}).get("props", {}).get("pageProps", {})
                    slug_listings = [parse_listing_from_nextdata(raw) for raw in pp2.get("listings", [])]
                    slug_listings = [p for p in slug_listings if p.get("make")]
                    if slug_listings:
                        all_listings = slug_listings
                        total_count = pp2.get("numberOfResults", len(slug_listings))
                        url = slug_url  # use slug for pages 2+ too
                        logger.info(f"[as24] slug fallback recovered {len(all_listings)} listings (total: {total_count})")

        # ── Pages 2+: fetch in parallel with staggered delays ─────────────
        if pages_to_fetch > 1 and len(all_listings) < max_results:
            async def _fetch_page(page_num: int) -> list[dict]:
                await rate_acquire("autoscout24.com")
                p_url = re.sub(r'page=\d+', f'page={page_num}', url) if 'page=' in url else url + f'&page={page_num}'
                try:
                    r = await client.get(p_url)
                    if r.status_code == 429:
                        # Exponential backoff on rate limit
                        await asyncio.sleep(random.uniform(3, 8))
                        r = await client.get(p_url)
                    if r.status_code != 200:
                        return []
                    nd2 = _extract_next_data(r.text)
                    if not nd2:
                        return []
                    items = []
                    for raw in nd2.get("props", {}).get("pageProps", {}).get("listings", []):
                        p = parse_listing_from_nextdata(raw)
                        if p.get("make"):
                            items.append(p)
                    return items
                except Exception as e:
                    logger.debug(f"[as24] Page {page_num} failed: {e}")
                    return []

            page_tasks = [_fetch_page(p) for p in range(2, pages_to_fetch + 1)]
            page_results = await asyncio.gather(*page_tasks)
            for page_listings in page_results:
                all_listings.extend(page_listings)

            logger.info(f"[as24] Pages 2-{pages_to_fetch}: +{sum(len(r) for r in page_results)} listings")

        return all_listings[:max_results], total_count

    finally:
        if HAS_CURL_CFFI:
            await client.close()
        else:
            await client.aclose()
