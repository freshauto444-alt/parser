#!/usr/bin/env python3
"""
DB Warmup Script — pre-populates Supabase with real car listings from all 4 sites.
Run ONCE before site launch to seed the price guide and cache.

Searches 50+ popular models across multiple price segments.
Each model → parallel scrape from AS24 + Bytbil + Blocket + Mobile.de → save to DB.

Usage:
    python -m parsers.warmup              # Full warmup (~50 models)
    python -m parsers.warmup --fast       # Quick warmup (~15 top models)
    python -m parsers.warmup --segment luxury  # Only luxury segment

Expected: ~3000-5000 cars in DB after full warmup (~15 min).
"""

import asyncio
import sys
import time
from loguru import logger

# All segments with popular models
SEGMENTS = {
    "budget": [
        {"brand": "skoda", "model": "octavia", "year_from": 2018},
        {"brand": "skoda", "model": "karoq", "year_from": 2018},
        {"brand": "skoda", "model": "superb", "year_from": 2018},
        {"brand": "dacia", "model": "duster", "year_from": 2018},
        {"brand": "hyundai", "model": "i30", "year_from": 2018},
        {"brand": "kia", "model": "ceed", "year_from": 2018},
        {"brand": "ford", "model": "focus", "year_from": 2018},
        {"brand": "peugeot", "model": "308", "year_from": 2018},
    ],
    "mainstream": [
        {"brand": "volkswagen", "model": "golf", "year_from": 2019},
        {"brand": "volkswagen", "model": "passat", "year_from": 2019},
        {"brand": "volkswagen", "model": "tiguan", "year_from": 2019},
        {"brand": "volkswagen", "model": "t-roc", "year_from": 2019},
        {"brand": "toyota", "model": "rav4", "year_from": 2019},
        {"brand": "toyota", "model": "corolla", "year_from": 2019},
        {"brand": "hyundai", "model": "tucson", "year_from": 2019},
        {"brand": "kia", "model": "sportage", "year_from": 2019},
        {"brand": "mazda", "model": "cx-5", "year_from": 2019},
        {"brand": "nissan", "model": "qashqai", "year_from": 2019},
        {"brand": "peugeot", "model": "3008", "year_from": 2019},
    ],
    "premium": [
        {"brand": "bmw", "model": "3er", "year_from": 2019},
        {"brand": "bmw", "model": "5er", "year_from": 2019},
        {"brand": "bmw", "model": "x3", "year_from": 2019},
        {"brand": "bmw", "model": "x5", "year_from": 2019},
        {"brand": "audi", "model": "a4", "year_from": 2019},
        {"brand": "audi", "model": "a6", "year_from": 2019},
        {"brand": "audi", "model": "q5", "year_from": 2019},
        {"brand": "mercedes-benz", "model": "c-klasse", "year_from": 2019},
        {"brand": "mercedes-benz", "model": "e-klasse", "year_from": 2019},
        {"brand": "mercedes-benz", "model": "glc", "year_from": 2019},
        {"brand": "volvo", "model": "v60", "year_from": 2019},
        {"brand": "volvo", "model": "xc60", "year_from": 2019},
        {"brand": "volvo", "model": "xc40", "year_from": 2019},
    ],
    "sport": [
        {"brand": "bmw", "model": "4er", "year_from": 2019},
        {"brand": "bmw", "model": "m3", "year_from": 2019, "price_from": 40000},
        {"brand": "audi", "model": "a5", "year_from": 2019},
        {"brand": "mercedes-benz", "model": "cla", "year_from": 2019},
        {"brand": "ford", "model": "mustang", "year_from": 2018},
        {"brand": "toyota", "model": "supra", "year_from": 2019},
        {"brand": "alfa romeo", "model": "giulia", "year_from": 2018},
    ],
    "luxury": [
        {"brand": "bmw", "model": "7er", "year_from": 2019, "price_from": 40000},
        {"brand": "mercedes-benz", "model": "s-klasse", "year_from": 2019, "price_from": 45000},
        {"brand": "audi", "model": "a8", "year_from": 2019, "price_from": 40000},
        {"brand": "porsche", "model": "cayenne", "year_from": 2018},
        {"brand": "porsche", "model": "macan", "year_from": 2018},
        {"brand": "porsche", "model": "911", "year_from": 2018, "price_from": 60000},
        {"brand": "land rover", "model": "range rover", "year_from": 2018, "price_from": 40000},
        {"brand": "jaguar", "model": "f-pace", "year_from": 2018},
        {"brand": "lexus", "model": "nx", "year_from": 2018},
        {"brand": "tesla", "model": "model 3", "year_from": 2019},
        {"brand": "tesla", "model": "model y", "year_from": 2020},
    ],
}

# Quick mode — top 15 models
FAST_MODELS = [
    {"brand": "bmw", "model": "3er", "year_from": 2019},
    {"brand": "bmw", "model": "x5", "year_from": 2019},
    {"brand": "audi", "model": "a4", "year_from": 2019},
    {"brand": "audi", "model": "a6", "year_from": 2019},
    {"brand": "mercedes-benz", "model": "c-klasse", "year_from": 2019},
    {"brand": "mercedes-benz", "model": "e-klasse", "year_from": 2019},
    {"brand": "volkswagen", "model": "golf", "year_from": 2019},
    {"brand": "volkswagen", "model": "tiguan", "year_from": 2019},
    {"brand": "volvo", "model": "xc60", "year_from": 2019},
    {"brand": "toyota", "model": "rav4", "year_from": 2019},
    {"brand": "skoda", "model": "octavia", "year_from": 2019},
    {"brand": "porsche", "model": "cayenne", "year_from": 2018},
    {"brand": "hyundai", "model": "tucson", "year_from": 2019},
    {"brand": "tesla", "model": "model 3", "year_from": 2019},
    {"brand": "kia", "model": "sportage", "year_from": 2019},
]


async def warmup_model(params: dict, idx: int, total: int) -> int:
    """Search one model across all 4 sites and save to DB."""
    from .orchestrator import search
    from .db import save
    from supabase import create_client
    import os

    brand = params.get("brand", "")
    model = params.get("model", "")
    label = f"{brand} {model}"

    logger.info(f"[warmup] [{idx+1}/{total}] {label}...")

    try:
        cars = await search(params, max_results=30)
        if cars:
            supabase = create_client(
                os.getenv("SUPABASE_URL"),
                os.getenv("SUPABASE_SERVICE_KEY"),
            )
            saved, updated, errors = save(
                cars, supabase,
                source_type="parser_hot",
                expires_hours=7 * 24,  # 7 days
            )
            logger.success(f"[warmup] [{idx+1}/{total}] {label}: {len(cars)} found, {saved} saved, {updated} updated")
            return len(cars)
        else:
            logger.warning(f"[warmup] [{idx+1}/{total}] {label}: 0 cars found")
            return 0
    except Exception as e:
        logger.error(f"[warmup] [{idx+1}/{total}] {label}: {e}")
        return 0


async def run_warmup(models: list[dict], batch_size: int = 3):
    """Run warmup for a list of models, in batches to avoid rate limiting."""
    total = len(models)
    total_cars = 0

    logger.info(f"[warmup] Starting warmup: {total} models in batches of {batch_size}")
    start = time.time()

    for i in range(0, total, batch_size):
        batch = models[i:i + batch_size]
        results = await asyncio.gather(
            *[warmup_model(m, i + j, total) for j, m in enumerate(batch)],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, int):
                total_cars += r

        # Pause between batches to avoid rate limits
        if i + batch_size < total:
            await asyncio.sleep(5)

    elapsed = time.time() - start
    logger.success(f"[warmup] DONE: {total_cars} cars from {total} models in {elapsed:.0f}s")
    return total_cars


async def main():
    from dotenv import load_dotenv
    load_dotenv()

    # Parse args
    fast = "--fast" in sys.argv
    segment = None
    for arg in sys.argv:
        if arg.startswith("--segment="):
            segment = arg.split("=")[1]

    if fast:
        models = FAST_MODELS
        logger.info("[warmup] FAST mode: 15 top models")
    elif segment and segment in SEGMENTS:
        models = SEGMENTS[segment]
        logger.info(f"[warmup] Segment '{segment}': {len(models)} models")
    else:
        models = []
        for seg_models in SEGMENTS.values():
            models.extend(seg_models)
        logger.info(f"[warmup] FULL mode: {len(models)} models across {len(SEGMENTS)} segments")

    await run_warmup(models, batch_size=2)

    # Cleanup
    from .browser_pool import BrowserPool
    await BrowserPool.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
