# parsers/main.py
# CLI entry point + hot deals cron + worker loop.
# All search logic delegates to orchestrator.py.

import asyncio
import os
import sys
from typing import Optional
from datetime import datetime, timezone

from loguru import logger
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

from .base import ParsedCar
from .db import save, cleanup_expired
from .browser_pool import BrowserPool
from .orchestrator import search, deduplicate, SOURCES
from .config import (
    MIN_YEAR, MIN_PRICE_EUR, MAX_MILEAGE, SEK_TO_EUR,
    HOT_SEARCHES, HOT_SEARCHES_PER_MODEL,
    MAX_HOT_DEALS, MAX_PER_SOURCE,
    HOT_DEALS_EXPIRES_HOURS, CUSTOM_SEARCH_EXPIRES_HOURS,
)


# ══════════════════════════════════════════════════════════════════════════════
#  HOT DEALS — cron job (every 6h)
# ══════════════════════════════════════════════════════════════════════════════

async def run_hot_deals() -> list[ParsedCar]:
    logger.info("═══ Hot Deals ═══")
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    cleanup_expired(supabase, "parser_hot")

    all_cars: list[ParsedCar] = []

    # Scrape each hot model via orchestrator (parallel, cached)
    for params in HOT_SEARCHES:
        try:
            cars = await search(params, max_results=HOT_SEARCHES_PER_MODEL)
            all_cars.extend(cars)
            logger.info(f"[hot] {params['brand']} {params['model']}: {len(cars)} авто")
        except Exception as e:
            logger.error(f"[hot] {params}: {e}")

    unique = deduplicate(all_cars)
    unique.sort(key=lambda c: c.score, reverse=True)
    unique = unique[:MAX_HOT_DEALS]
    logger.info(f"[hot] Total: {len(unique)} unique")

    save(unique, supabase, source_type="parser_hot", expires_hours=HOT_DEALS_EXPIRES_HOURS)
    return unique


# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOM SEARCH — on-demand via API
# ══════════════════════════════════════════════════════════════════════════════

async def run_custom_search(
    params: dict,
    client_order_id: Optional[str] = None,
) -> list[ParsedCar]:
    logger.info(f"═══ Custom Search: {params.get('brand')} {params.get('model')} ═══")
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    if client_order_id:
        _update_order_status(supabase, client_order_id, "searching")

    # Use orchestrator for parallel, cached search
    max_res = params.get("max_results", 20)
    results = await search(params, max_results=max_res)

    # Filter by price/year (orchestrator passes filters to source sites,
    # but double-check here for safety)
    price_from = max(params.get("price_from") or 0, MIN_PRICE_EUR)
    price_to = params.get("price_to")
    year_from = params.get("year_from")
    year_to = params.get("year_to")

    filtered = [
        c for c in results
        if (c.price_eur or 0) >= price_from
        and (price_to is None or (c.price_eur or 0) <= price_to)
        and (year_from is None or not c.year or c.year >= year_from)
        and (year_to is None or not c.year or c.year <= year_to)
    ]
    filtered.sort(key=lambda c: c.score, reverse=True)

    # Save to DB
    saved, updated, errors = save(
        filtered, supabase,
        source_type="parser_featured",
        client_order_id=client_order_id,
        expires_hours=CUSTOM_SEARCH_EXPIRES_HOURS,
    )

    if client_order_id:
        status = "found" if (saved + updated) > 0 else "empty"
        _update_order_status(supabase, client_order_id, status)

    return filtered


# ══════════════════════════════════════════════════════════════════════════════
#  WORKER — job queue processor
# ══════════════════════════════════════════════════════════════════════════════

async def run_worker(poll_interval: int = 5):
    logger.info("═══ Worker started ═══")
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    while True:
        try:
            result = (
                supabase.table("search_jobs")
                .select("*").eq("status", "pending")
                .order("created_at", desc=False).limit(1)
                .execute()
            )
            if not result.data:
                await asyncio.sleep(poll_interval)
                continue

            job = result.data[0]
            job_id = job["id"]
            logger.info(f"[worker] Processing job {job_id}")

            supabase.table("search_jobs").update({
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", job_id).execute()

            try:
                results = await run_custom_search(
                    params=job.get("params") or {},
                    client_order_id=job.get("client_order_id"),
                )
                supabase.table("search_jobs").update({
                    "status": "done",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "results_count": len(results),
                }).eq("id", job_id).execute()
            except Exception as e:
                supabase.table("search_jobs").update({
                    "status": "failed",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": str(e)[:500],
                }).eq("id", job_id).execute()
                logger.error(f"[worker] Job {job_id} failed: {e}")

        except Exception as e:
            logger.error(f"[worker] Poll error: {e}")
            await asyncio.sleep(poll_interval * 2)

        await asyncio.sleep(1)


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _update_order_status(supabase, client_order_id: str, status: str):
    try:
        supabase.table("client_orders").update({
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", client_order_id).execute()
    except Exception as e:
        logger.warning(f"[order] Status update failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

async def _run(coro):
    try:
        return await coro
    finally:
        await BrowserPool.shutdown()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "hot"

    if mode == "hot":
        asyncio.run(_run(run_hot_deals()))

    elif mode == "search":
        asyncio.run(_run(run_custom_search(params={
            "brand": sys.argv[2] if len(sys.argv) > 2 else "bmw",
            "model": sys.argv[3] if len(sys.argv) > 3 else "3er",
            "year_from": int(sys.argv[4]) if len(sys.argv) > 4 else 2018,
            "price_to": int(sys.argv[5]) if len(sys.argv) > 5 else None,
            "max_results": 20,
        })))

    elif mode == "worker":
        asyncio.run(_run(run_worker()))

    elif mode == "warmup":
        from .warmup import main as warmup_main
        asyncio.run(warmup_main())

    elif mode == "scan-prices":
        from .price_scanner import main as scanner_main
        asyncio.run(scanner_main())

    elif mode == "test-as24":
        url = sys.argv[2] if len(sys.argv) > 2 else ""
        from .parser_autoscout24 import test_one
        asyncio.run(test_one(url))

    elif mode == "taxonomy":
        # Re-harvest the AS24 taxonomy (brands+groups, optionally motors) into
        # Supabase so model→cat resolution stays complete (a missing group → 0
        # AS24 results, see as24_taxonomy.resolve_cat). Schedule weekly.
        #   python3 -m parsers.main taxonomy [phase] [brand]
        #     phase: 1 (default — brands+groups, ~10min) | 2 (motors, ~1h) | all
        from .harvest_as24_taxonomy import run_phase1, run_phase2
        phase = sys.argv[2] if len(sys.argv) > 2 else "1"
        brand = sys.argv[3] if len(sys.argv) > 3 else None
        if phase in ("1", "all"):
            asyncio.run(run_phase1(brand))
        if phase in ("2", "all"):
            asyncio.run(run_phase2(brand))
