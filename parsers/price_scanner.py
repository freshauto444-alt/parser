#!/usr/bin/env python3
"""
Price Scanner v2 — uses Mobile.de Reference API (FREE, no auth) to get ALL makes+models,
then checks AutoScout24 __NEXT_DATA__ for real prices per model+year.

Strategy:
  1. Mobile.de refdata API → get ALL makes (178) + ALL models per make
  2. Filter to popular makes (top 40)
  3. For each model+year: quick AS24 search → extract price range
  4. Save to price_memory

Speed: ~1 req/sec to AS24 = ~200 models × 6 years = ~1200 requests = ~20 min

Usage:
    python -m parsers.price_scanner                    # All popular makes
    python -m parsers.price_scanner --brand bmw        # Only BMW
    python -m parsers.price_scanner --fast              # Top 15 makes only
"""

import asyncio
import re
import sys
import time
from typing import Optional
from loguru import logger

import httpx

# Mobile.de reference API — FREE, no auth
REFDATA_URL = "https://services.mobile.de/refdata"

# Popular makes to scan (rest are too niche)
POPULAR_MAKES = {
    "BMW", "AUDI", "MERCEDES_BENZ", "VW", "VOLVO", "TOYOTA", "HONDA",
    "MAZDA", "SKODA", "FORD", "OPEL", "PEUGEOT", "RENAULT", "HYUNDAI",
    "KIA", "NISSAN", "PORSCHE", "TESLA", "MINI", "SEAT", "CUPRA",
    "CITROEN", "SUBARU", "LEXUS", "JAGUAR", "LAND_ROVER", "JEEP",
    "FIAT", "ALFA ROMEO", "DACIA", "SUZUKI", "MITSUBISHI",
    "GENESIS", "DS", "CHEVROLET", "CHRYSLER", "DODGE",
}

TOP_15_MAKES = {
    "BMW", "AUDI", "MERCEDES_BENZ", "VW", "VOLVO", "TOYOTA",
    "SKODA", "FORD", "HYUNDAI", "KIA", "PORSCHE", "TESLA",
    "PEUGEOT", "RENAULT", "NISSAN",
}

YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024]

# Make key → display name
MAKE_DISPLAY = {
    "MERCEDES_BENZ": "Mercedes-Benz",
    "ALFA ROMEO": "Alfa Romeo",
    "LAND_ROVER": "Land Rover",
    "VW": "Volkswagen",
}


async def get_all_makes(client: httpx.AsyncClient) -> list[str]:
    """Get all car makes from Mobile.de reference API."""
    resp = await client.get(f"{REFDATA_URL}/classes/Car/makes")
    return re.findall(r'key="([^"]+)"', resp.text)


async def get_models_for_make(client: httpx.AsyncClient, make: str) -> list[str]:
    """Get all models for a make from Mobile.de reference API."""
    resp = await client.get(f"{REFDATA_URL}/classes/Car/makes/{make}/models")
    if resp.status_code != 200:
        return []
    return re.findall(r'key="([^"]+)"', resp.text)


def make_display(make_key: str) -> str:
    """Convert Mobile.de make key to display name."""
    if make_key in MAKE_DISPLAY:
        return MAKE_DISPLAY[make_key]
    return make_key.replace("_", " ").title()


def model_to_as24_slug(make_key: str, model_key: str) -> str:
    """Convert Mobile.de model key to AS24 URL slug."""
    # Mobile.de uses "320", "A4", "Golf" — AS24 uses "3er", "a4", "golf"
    # Map common BMW series
    bmw_series = re.match(r'^(\d)\d{2}', model_key)
    if make_key == "BMW" and bmw_series:
        return f"{bmw_series.group(1)}er"

    # Clean up model key for URL
    slug = model_key.lower().replace(" ", "-").replace("_", "-")
    return slug


async def scan_model_year_fast(make: str, model_slug: str, year: int) -> Optional[dict]:
    """Quick AS24 price check — fetch one page, extract prices from __NEXT_DATA__."""
    from .as24_http import search as as24_search

    params = {
        "brand": make.lower().replace("_", "-").replace(" ", "-"),
        "model": model_slug,
        "year_from": year,
        "year_to": year,
    }

    try:
        listings, total = await as24_search(params, max_results=10, sort="standard")
        if not listings or len(listings) < 2:
            return None

        prices = sorted([l["price"] for l in listings if l.get("price") and 1000 < l["price"] < 500000])
        if len(prices) < 2:
            return None

        fuels = [l.get("fuel") for l in listings if l.get("fuel")]
        fuel_counts = {}
        for f in fuels:
            fuel_counts[f] = fuel_counts.get(f, 0) + 1
        main_fuel = max(fuel_counts, key=fuel_counts.get) if fuel_counts else None

        # Trim outliers
        if len(prices) > 4:
            trim = max(1, len(prices) // 5)
            prices = prices[trim:-trim]

        return {
            "min": int(prices[0]),
            "max": int(prices[-1]),
            "median": int(prices[len(prices) // 2]),
            "count": len(listings),
            "fuel": main_fuel,
            "total_on_market": total,
        }
    except Exception as e:
        logger.debug(f"[scanner] {make} {model_slug} {year}: {e}")
        return None


async def run_scanner(makes_filter: set[str] = POPULAR_MAKES, years: list[int] = YEARS):
    """Main scanner: get all models from Mobile.de, check prices on AS24."""
    from .price_memory import record_prices

    logger.info("[scanner] Fetching makes and models from Mobile.de...")

    async with httpx.AsyncClient(timeout=10) as client:
        all_makes = await get_all_makes(client)
        filtered_makes = [m for m in all_makes if m in makes_filter]
        logger.info(f"[scanner] {len(filtered_makes)} makes to scan (from {len(all_makes)} total)")

        total_records = 0

        for make_key in filtered_makes:
            models = await get_models_for_make(client, make_key)
            display = make_display(make_key)

            # Deduplicate BMW series: 316, 318, 320, 325, 330 → just "3er"
            seen_slugs = set()
            unique_models = []
            for model_key in models:
                slug = model_to_as24_slug(make_key, model_key)
                if slug not in seen_slugs:
                    seen_slugs.add(slug)
                    unique_models.append((model_key, slug))

            logger.info(f"[scanner] {display}: {len(unique_models)} unique models (from {len(models)} total)")

            for model_key, slug in unique_models:
                # Scan all years in parallel for this model
                tasks = [scan_model_year_fast(make_key, slug, y) for y in years]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for i, result in enumerate(results):
                    if isinstance(result, Exception) or result is None:
                        continue

                    year = years[i]
                    # Record into price_memory
                    fake_cars = [
                        {"make": display, "model": slug, "price": result["min"],
                         "year": year, "fuel": result.get("fuel"), "mileage": None},
                        {"make": display, "model": slug, "price": result["max"],
                         "year": year, "fuel": result.get("fuel"), "mileage": None},
                    ]
                    try:
                        record_prices(fake_cars, source="scanner")
                    except Exception:
                        pass

                    total_records += 1
                    logger.info(
                        f"[scanner] {display} {slug} {year}: "
                        f"€{result['min']:,}-{result['max']:,} "
                        f"({result['count']} cars, {result.get('total_on_market', '?')} on market)"
                    )

                # Small pause between models to avoid rate limit
                await asyncio.sleep(1)

        logger.success(f"[scanner] DONE: {total_records} price records from {len(filtered_makes)} makes")


async def main():
    from dotenv import load_dotenv
    load_dotenv()

    makes = POPULAR_MAKES

    if "--fast" in sys.argv:
        makes = TOP_15_MAKES
        logger.info("[scanner] FAST mode: top 15 makes")
    elif "--brand" in sys.argv:
        idx = sys.argv.index("--brand") + 1
        if idx < len(sys.argv):
            brand = sys.argv[idx].upper().replace("-", "_")
            makes = {brand}
            logger.info(f"[scanner] Single brand: {brand}")

    await run_scanner(makes)

    from .browser_pool import BrowserPool
    await BrowserPool.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
