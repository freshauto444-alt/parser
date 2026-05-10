# parsers/parser_freshauto.py
# freshauto.com.ua — dealer's own inventory. WordPress REST API (product CPT).
# Used to SEED the catalog from the dealer's existing site, not a live marketplace.
#
# Endpoint shape (verified 2026-04):
#   GET /wp-json/wp/v2/product?per_page=100&page=N
#   fields we need: link, title.rendered, excerpt.rendered, acf.{mileage,year,engine,images[]}
#
# Price is not exposed via REST — must be scraped from the detail HTML.
# Body/fuel/transmission/drive come from excerpt (structured prefix).

import re
import html as html_mod
from typing import Optional

import aiohttp
from loguru import logger

from .base import (
    ParsedCar,
    normalize_make, normalize_model, normalize_fuel, normalize_transmission,
    normalize_color, normalize_body_type, normalize_drive,
    is_known_brand, calc_score, decode_html, FUEL_UA,
)

BASE = "https://freshauto.com.ua"
API = f"{BASE}/wp-json/wp/v2/product"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ── excerpt parser ──────────────────────────────────────────────────────────
# Excerpt prefix format observed in the wild:
#   "<p>Ford Transit 2020 у FreshAuto: Дизель, 2.0 л, Механіка, Задній, 125 000 км. ..."
# Order is stable: FUEL, DISPLACEMENT, TRANSMISSION, DRIVE, MILEAGE.
_EXCERPT_RE = re.compile(
    r"у\s+FreshAuto:\s*"
    r"(?P<fuel>[^,]+),\s*"
    r"(?P<displacement>[\d.]+)\s*л,\s*"
    r"(?P<trans>[^,]+),\s*"
    r"(?P<drive>[^,]+),\s*"
    r"(?P<mileage>[\d\s]+)\s*км",
    re.UNICODE,
)

# Title format: "<Make> <Model...> <Year>"
_TITLE_RE = re.compile(r"^(\S+)\s+(.+?)\s+(\d{4})$")

# Price in detail HTML — freshauto.com.ua renders in USD, e.g. "32 500 $"
# After HTML entity decoding and tag stripping we match the number near $|€|USD|EUR.
_PRICE_NEAR_CUR_RE = re.compile(
    r"([\d]{1,3}(?:[\s\xa0][\d]{3})+|\d{4,6})\s*(?:\$|€|USD|EUR)", re.UNICODE,
)
# USD→EUR conversion rate (approximate, for dealer-site seeding only).
USD_TO_EUR = 0.92


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", html_mod.unescape(s or "")).strip()


def _num(s: str) -> Optional[int]:
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    return int(digits) if digits else None


def _parse_title(title: str) -> tuple[str, str, Optional[int]]:
    t = _strip_html(title)
    m = _TITLE_RE.match(t)
    if not m:
        return t, "", None
    return m.group(1), m.group(2), int(m.group(3))


def _parse_excerpt(excerpt_html: str) -> dict:
    txt = _strip_html(excerpt_html)
    m = _EXCERPT_RE.search(txt)
    if not m:
        return {}
    return {
        "fuel_raw": m.group("fuel").strip(),
        "displacement": m.group("displacement").strip(),
        "trans_raw": m.group("trans").strip(),
        "drive_raw": m.group("drive").strip(),
        "mileage_km": _num(m.group("mileage")),
    }


# UA → internal canonical mapping for freshauto-specific strings
_FUEL_MAP = {
    "Бензин": "Petrol", "Дизель": "Diesel", "Електро": "Electric",
    "Гібрид": "Hybrid", "Газ": "LPG",
}
_TRANS_MAP = {
    "Автомат": "Automatic", "Механіка": "Manual",
    "Робот": "DCT", "Варіатор": "CVT", "Типтронік": "Automatic",
}
_DRIVE_MAP = {
    "Передній": "FWD", "Задній": "RWD",
    "Повний": "AWD", "Повний привід": "AWD", "4WD": "AWD",
}


async def _fetch_price(session: aiohttp.ClientSession, url: str) -> Optional[float]:
    """Scrape price from detail page HTML. Best-effort; returns None on failure."""
    try:
        async with session.get(url, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            body = await r.text()
    except Exception as e:
        logger.debug(f"freshauto: price fetch failed for {url}: {e}")
        return None
    # Decode HTML entities (freshauto uses &#36; for $) then strip tags so
    # "<span>32 500</span><span>$</span>" collapses into "32 500 $".
    text = html_mod.unescape(body)
    # Limit search to the first "price" class block to avoid picking up footer junk.
    m_block = re.search(r'class="[^"]*price[^"]*"[^>]*>(.{0,1500})', text, flags=re.DOTALL)
    scope = m_block.group(1) if m_block else text
    scope = re.sub(r"<[^>]+>", " ", scope)
    scope = re.sub(r"\s+", " ", scope).strip()
    m = _PRICE_NEAR_CUR_RE.search(scope)
    if not m:
        return None
    n = _num(m.group(1))
    if not n:
        return None
    # Determine currency from the matched suffix context
    suffix = scope[m.end() - 6:m.end() + 6]
    is_eur = "€" in suffix or "EUR" in suffix
    return float(n) if is_eur else float(n) * USD_TO_EUR


def _build_car(item: dict, price_eur: Optional[float]) -> Optional[ParsedCar]:
    link = item.get("link") or ""
    title_html = (item.get("title") or {}).get("rendered", "")
    excerpt_html = (item.get("excerpt") or {}).get("rendered", "")
    acf = item.get("acf") or {}

    make_raw, model_raw, title_year = _parse_title(title_html)
    if not make_raw or not is_known_brand(make_raw):
        # Not a recognized car brand — skip
        return None

    make = normalize_make(make_raw)
    model = normalize_model(model_raw, make)

    ex = _parse_excerpt(excerpt_html)

    fuel_canonical = _FUEL_MAP.get(ex.get("fuel_raw", ""), None)
    fuel = normalize_fuel(fuel_canonical) if fuel_canonical else None
    trans = _TRANS_MAP.get(ex.get("trans_raw", ""), ex.get("trans_raw") or None)
    drive_canonical = _DRIVE_MAP.get(ex.get("drive_raw", ""), None)
    drive = normalize_drive(drive_canonical) if drive_canonical else None

    year = title_year
    if not year:
        y = acf.get("year")
        if y:
            try:
                year = int(str(y))
            except ValueError:
                year = None

    # ACF mileage is in thousands (e.g. "125" = 125 000 km) per observed data
    mileage_km = ex.get("mileage_km")
    if mileage_km is None:
        raw_m = acf.get("mileage")
        if raw_m:
            try:
                mileage_km = int(float(str(raw_m))) * 1000
            except ValueError:
                mileage_km = None

    displacement = ex.get("displacement") or acf.get("engine") or ""
    engine_str = f"{displacement}L {fuel}" if displacement and fuel else (displacement or None)

    images = [img.get("url") for img in (acf.get("images") or []) if isinstance(img, dict) and img.get("url")]

    external_id = (item.get("slug") or str(item.get("id") or "")).strip()
    if not external_id:
        return None

    car = ParsedCar(
        source_site="freshauto",
        source_url=link,
        external_id=f"freshauto:{external_id}",
        make=make,
        model=model,
        year=year,
        price_eur=price_eur,
        mileage_km=mileage_km,
        fuel=fuel,
        fuel_ua=FUEL_UA.get(fuel) if fuel else None,
        transmission=trans,
        drive=drive,
        engine=engine_str,
        image=images[0] if images else None,
        gallery=images,
        country="Ukraine",
        location="Україна",
        condition="Used",
        seller_type="dealer",
        dealer_name="Fresh Auto",
        dealer_website=BASE,
        title_line=f"{make} {model} {year}".strip() if year else f"{make} {model}".strip(),
    )
    car.score = calc_score(
        year=car.year,
        mileage=car.mileage_km,
        price_eur=car.price_eur,
        has_image=bool(car.image),
        gallery_count=len(car.gallery),
        has_drive=bool(car.drive),
        seller_type=car.seller_type,
    )
    return car


async def parse_freshauto(
    session: Optional[aiohttp.ClientSession] = None,
    max_pages: int = 5,
    per_page: int = 50,
    fetch_prices: bool = True,
) -> list[ParsedCar]:
    """Fetch all products via WP REST API, optionally enrich with prices from detail pages.

    Args:
        session: reuse outer aiohttp session, or None to create a local one.
        max_pages: cap pagination (API returns up to 100 items per page).
        per_page: WP REST per_page param (max 100).
        fetch_prices: if True, fetch each detail page to extract price.

    Returns: list[ParsedCar]
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(headers={"User-Agent": UA})

    results: list[ParsedCar] = []
    try:
        for page in range(1, max_pages + 1):
            url = f"{API}?per_page={per_page}&page={page}"
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 400:
                        # Out of range
                        break
                    if r.status != 200:
                        logger.warning(f"freshauto: page {page} HTTP {r.status}")
                        break
                    items = await r.json()
            except Exception as e:
                logger.warning(f"freshauto: page {page} fetch failed: {e}")
                break

            if not items:
                break

            for item in items:
                price = None
                if fetch_prices:
                    price = await _fetch_price(session, item.get("link", ""))
                car = _build_car(item, price)
                if car:
                    results.append(car)

            if len(items) < per_page:
                break

        logger.info(f"freshauto: parsed {len(results)} listings")
    finally:
        if own_session:
            await session.close()

    return results


# CLI quickcheck: python -m parsers.parser_freshauto
if __name__ == "__main__":
    import asyncio
    import json

    async def main():
        cars = await parse_freshauto(max_pages=1, per_page=5, fetch_prices=False)
        for c in cars:
            print(json.dumps({
                "make": c.make, "model": c.model, "year": c.year,
                "fuel": c.fuel, "trans": c.transmission, "drive": c.drive,
                "mileage_km": c.mileage_km, "price_eur": c.price_eur,
                "image": c.image, "url": c.source_url,
            }, ensure_ascii=False))

    asyncio.run(main())
