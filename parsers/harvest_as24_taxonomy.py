"""
AS24 taxonomy harvester — populates as24_brands / as24_groups / as24_motors
in Supabase so the live parser can build cat-based search URLs instead of
the slug-based ones (which returned 5× fewer results, see Kia Ceed test).

Phase 1 (brands + groups, ~5-10 min, ~285 requests):
  • Bootstrap brand list from one /lst page taxonomy.makes
  • For each brand: GET /lst/{slug}, parse __NEXT_DATA__
  • Extract makeId + all modelGroups
  • Upsert into as24_brands + as24_groups

Phase 2 (motor types, ~30-60 min, ~3000-5000 requests):
  • For each (brand, group): GET /lst/{slug}?cat=ma{N}gr{N}
  • Extract (p_id, variant_id, generation_id, motortype_id) tuples and
    matching listing titles from HTML
  • Aggregate per motortype_id (most common label wins)
  • Upsert into as24_motors

Usage:
  python -m parsers.harvest_as24_taxonomy --phase 1
  python -m parsers.harvest_as24_taxonomy --phase 2
  python -m parsers.harvest_as24_taxonomy --phase 1 --brand kia        # one brand
  python -m parsers.harvest_as24_taxonomy --phase 2 --brand mercedes-benz
  python -m parsers.harvest_as24_taxonomy --phase all                  # 1 then 2

Env vars (already used by other parser scripts):
  SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from typing import Optional

import httpx
from loguru import logger

# ── Config ────────────────────────────────────────────────────────────────────
BASE = "https://www.autoscout24.de"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml",
}

# Phase 1 brands: discovered dynamically from /lst/bmw taxonomy.makes (~285).
# Phase 2 max concurrency keeps us under AS24's per-IP rate limit while still
# finishing in well under an hour.
PHASE1_CONCURRENCY = 6
PHASE2_CONCURRENCY = 4

# Some "makes" in the taxonomy are tractor / agricultural / very-rare brands
# that 404 on /lst/{slug}. Skip them quietly instead of polluting logs.
SKIP_BRANDS = {"sonstige", "andere", "other"}


# ── Helpers ───────────────────────────────────────────────────────────────────
def slugify_brand(label: str) -> str:
    """Mirror as24_http.py logic: 'Aston Martin' → 'aston-martin'."""
    return re.sub(r"\s+", "-", label.lower()).strip("-")


def normalize_label(label: str) -> str:
    return label.lower().strip()


def supabase_client():
    url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("Missing SUPABASE_URL / SUPABASE_SERVICE_KEY env vars")
    from supabase import create_client
    return create_client(url, key)


async def fetch_next_data(client: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        r = await client.get(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError as e:
        logger.debug(f"fetch error {url}: {e}")
        return None
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        logger.debug(f"fetch HTTP {r.status_code} {url}")
        return None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>', r.text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        logger.debug(f"__NEXT_DATA__ JSON parse failed for {url}: {e}")
        return None


async def fetch_html(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    return r.text


# ── Phase 0: discover brand list ──────────────────────────────────────────────
async def discover_brands(client: httpx.AsyncClient) -> list[dict]:
    """Get the full brand list from one taxonomy fetch."""
    data = await fetch_next_data(client, f"{BASE}/lst/bmw")
    if not data:
        raise SystemExit("Bootstrap failed: /lst/bmw __NEXT_DATA__ missing")
    makes = data["props"]["pageProps"]["taxonomy"]["makes"]
    brands = []
    for _make_key, info in makes.items():
        label = info["label"]
        slug = slugify_brand(label)
        if slug in SKIP_BRANDS:
            continue
        brands.append({"slug": slug, "make_id": int(info["value"]), "label": label})
    return sorted(brands, key=lambda b: b["slug"])


# ── Phase 1: brand + model groups ─────────────────────────────────────────────
async def harvest_brand_groups(
    client: httpx.AsyncClient,
    supabase,
    brand: dict,
    semaphore: asyncio.Semaphore,
) -> tuple[int, int]:
    async with semaphore:
        slug = brand["slug"]
        tax_data = await fetch_next_data(client, f"{BASE}/lst/{slug}")
        if not tax_data:
            logger.warning(f"[{slug}] no __NEXT_DATA__ (likely 404)")
            return 0, 0

        try:
            tax = tax_data["props"]["pageProps"]["taxonomy"]
        except KeyError:
            logger.warning(f"[{slug}] unexpected page structure")
            return 0, 0

        # Upsert brand record (idempotent)
        supabase.table("as24_brands").upsert({
            "slug": slug,
            "make_id": brand["make_id"],
            "label": brand["label"],
        }, on_conflict="slug").execute()

        # Pull model groups for this brand from taxonomy.modelGroups[makeId]
        groups = tax.get("modelGroups", {}).get(str(brand["make_id"]), [])
        if not isinstance(groups, list):
            groups = []

        rows = [{
            "brand_slug": slug,
            "group_id": int(g["value"]),
            "label": g["label"],
            "label_norm": normalize_label(g["label"]),
        } for g in groups if "value" in g and "label" in g]

        # Batch upsert in chunks (Supabase request size limit)
        for i in range(0, len(rows), 100):
            chunk = rows[i:i + 100]
            supabase.table("as24_groups").upsert(
                chunk, on_conflict="brand_slug,group_id",
            ).execute()

        logger.info(f"[{slug}] brand + {len(rows)} groups")
        return 1, len(rows)


# ── Phase 2: motor types per group ────────────────────────────────────────────
LISTING_RX = re.compile(
    # AS24 listing card snippet — same DOM chunk holds the analytics data attribs
    # and the visible listing title within ~1KB of each other. Use non-greedy
    # match with reasonable distance so we pair them correctly.
    r'p_id:(\d+),\s*variant_id:(\d+),\s*generation_id:(\d+),\s*motortype_id:(\d+)'
    r'.{0,5000}?'
    r'ListItemTitle_title__\w+">([^<]+)<',
    re.DOTALL,
)

# Fallback: motor tuples without paired title (for listings with unusual DOM).
MOTOR_TUPLE_RX = re.compile(
    r'p_id:(\d+),\s*variant_id:(\d+),\s*generation_id:(\d+),\s*motortype_id:(\d+)'
)


def parse_motors_from_html(html: str, brand_label: str) -> list[dict]:
    """
    Extract motor types from a search-results HTML page.
    Returns aggregated rows per motortype_id with the most common model label.
    """
    paired = LISTING_RX.findall(html)
    all_tuples = MOTOR_TUPLE_RX.findall(html)

    # Group by motortype_id, accumulate variant/generation context + label votes.
    by_motor: dict[int, dict] = {}

    def _stash(mt_id: int, variant_id: int, generation_id: int, label: Optional[str]):
        entry = by_motor.setdefault(mt_id, {
            "variant_id": variant_id,
            "generation_id": generation_id,
            "labels": Counter(),
            "count": 0,
        })
        entry["count"] += 1
        if label:
            entry["labels"][label] += 1

    # Decode HTML entities + strip brand prefix from titles.
    brand_norm = brand_label.lower().replace("&amp;", "&")
    def _clean_label(raw_title: str) -> str:
        text = raw_title.replace("&#x27;", "'").replace("&amp;", "&").strip()
        # Strip leading brand if present
        lower = text.lower()
        if lower.startswith(brand_norm):
            text = text[len(brand_norm):].strip()
            text = text.lstrip("-/ ").strip()
        return text

    for p_id, variant_id, generation_id, mt_id, raw_title in paired:
        title = _clean_label(raw_title)
        _stash(int(mt_id), int(variant_id), int(generation_id), title or None)

    # Cover motor tuples we didn't manage to pair to a title.
    pair_keys = {(p_id, var, gen, mt) for p_id, var, gen, mt, _ in paired}
    for p_id, variant_id, generation_id, mt_id in all_tuples:
        if (p_id, variant_id, generation_id, mt_id) in pair_keys:
            continue
        _stash(int(mt_id), int(variant_id), int(generation_id), None)

    rows = []
    for mt_id, info in by_motor.items():
        most_common_label = info["labels"].most_common(1)
        label = most_common_label[0][0] if most_common_label else None
        rows.append({
            "motortype_id": mt_id,
            "variant_id": info["variant_id"],
            "generation_id": info["generation_id"],
            "listings_count": info["count"],
            "model_label": label,
            "model_label_norm": normalize_label(label) if label else None,
        })
    return rows


async def harvest_group_motors(
    client: httpx.AsyncClient,
    supabase,
    brand: dict,
    group: dict,
    semaphore: asyncio.Semaphore,
) -> int:
    """
    Make TWO requests per group with different sort orders. The default
    sort=standard shows the most popular motors (typical E 220 / E 300 /
    Ceed 1.4) but rare-but-important performance variants (E 63 AMG, E 53,
    Ceed GT) only appear on sort=price&desc=1 — they're the most expensive
    listings. Merging both sweeps catches the long tail.
    """
    async with semaphore:
        slug = brand["slug"]
        make_id = brand["make_id"]
        group_id = int(group["group_id"])
        base_url = f"{BASE}/lst/{slug}?cat=ma{make_id}gr{group_id}&ustate=N%2CU"

        combined_html_chunks: list[str] = []
        for sort_q in ("sort=standard", "sort=price&desc=1"):
            html = await fetch_html(client, f"{base_url}&{sort_q}")
            if html:
                combined_html_chunks.append(html)

        if not combined_html_chunks:
            logger.debug(f"[{slug}/gr{group_id}] empty html")
            return 0

        rows = parse_motors_from_html("\n".join(combined_html_chunks), brand["label"])
        if not rows:
            return 0

        full_rows = [{
            "brand_slug": slug,
            "group_id": group_id,
            **r,
        } for r in rows]

        for i in range(0, len(full_rows), 100):
            chunk = full_rows[i:i + 100]
            supabase.table("as24_motors").upsert(
                chunk, on_conflict="brand_slug,group_id,motortype_id",
            ).execute()

        logger.info(f"[{slug}/gr{group_id}] {len(full_rows)} motors")
        return len(full_rows)


# ── Orchestration ─────────────────────────────────────────────────────────────
async def run_phase1(brand_filter: Optional[str] = None) -> None:
    supabase = supabase_client()
    sem = asyncio.Semaphore(PHASE1_CONCURRENCY)
    async with httpx.AsyncClient(headers=HEADERS, http2=False) as client:
        brands = await discover_brands(client)
        if brand_filter:
            brands = [b for b in brands if b["slug"] == brand_filter]
            if not brands:
                logger.error(f"Brand '{brand_filter}' not in AS24 taxonomy")
                return
        logger.info(f"Phase 1: {len(brands)} brands to process")

        tasks = [
            harvest_brand_groups(client, supabase, b, sem)
            for b in brands
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    ok_brands = sum(1 for r in results if isinstance(r, tuple) and r[0])
    total_groups = sum(r[1] for r in results if isinstance(r, tuple))
    logger.info(f"Phase 1 done: {ok_brands}/{len(results)} brands, {total_groups} groups upserted")


def _read_all(supabase, table: str, page_size: int = 1000) -> list[dict]:
    """Supabase select() defaults to a 1000-row cap. Paginate via .range()."""
    out: list[dict] = []
    start = 0
    while True:
        res = supabase.table(table).select("*").range(start, start + page_size - 1).execute()
        chunk = res.data or []
        out.extend(chunk)
        if len(chunk) < page_size:
            break
        start += page_size
    return out


async def run_phase2(brand_filter: Optional[str] = None) -> None:
    supabase = supabase_client()
    # Read brand+group plan straight from the DB so phase 2 always reflects
    # what phase 1 just wrote. _read_all paginates past the 1000-row default.
    brands = _read_all(supabase, "as24_brands")
    if brand_filter:
        brands = [b for b in brands if b["slug"] == brand_filter]
    brands_by_slug = {b["slug"]: b for b in brands}

    all_groups = _read_all(supabase, "as24_groups")
    if brand_filter:
        all_groups = [g for g in all_groups if g["brand_slug"] == brand_filter]

    logger.info(f"Phase 2: {len(brands)} brands, {len(all_groups)} groups to process")
    if not all_groups:
        return

    sem = asyncio.Semaphore(PHASE2_CONCURRENCY)
    async with httpx.AsyncClient(headers=HEADERS, http2=False) as client:
        tasks = []
        for g in all_groups:
            b = brands_by_slug.get(g["brand_slug"])
            if not b:
                continue
            tasks.append(harvest_group_motors(client, supabase, b, g, sem))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    total_motors = sum(r for r in results if isinstance(r, int))
    failed = sum(1 for r in results if isinstance(r, Exception))
    logger.info(f"Phase 2 done: {total_motors} motors upserted, {failed} failures")


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest AS24 taxonomy → Supabase")
    parser.add_argument(
        "--phase", choices=["1", "2", "all"], default="1",
        help="1=brands+groups, 2=motors, all=both",
    )
    parser.add_argument(
        "--brand", default=None,
        help="Process only this brand slug (e.g. 'kia', 'mercedes-benz')",
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{level}</level> | {message}")

    if args.phase in ("1", "all"):
        asyncio.run(run_phase1(args.brand))
    if args.phase in ("2", "all"):
        asyncio.run(run_phase2(args.brand))


if __name__ == "__main__":
    main()
