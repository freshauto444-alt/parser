# parsers/orchestrator.py
# THE entry point for all car searches.
# Parallel scraping of 4 sites + cache-on-read + singleflight + SSE streaming.

import asyncio
import os
import re
from typing import AsyncIterator
from loguru import logger

from .base import ParsedCar
from .cache import cache_key, get_or_scrape
from .resilience import get_breaker
from .config import SEK_TO_EUR, DEFAULT_MAX_RESULTS, get_sek_to_eur


# ── Background DB save for user-search results ───────────────────────────────
# Every cold scrape persists its unique cars to Supabase with
# source_type='parser_search'. This way cars from ad-hoc /search/instant
# requests show up on the site's /order listing alongside the curated
# hot-deals batch, instead of vanishing after the 30-min memory cache expires.
#
# Fire-and-forget: the save runs as a detached asyncio task so the HTTP
# response to the user isn't delayed by Supabase round-trips.
_SEARCH_RESULT_TTL_HOURS = 48


async def _persist_search_results(cars: list[ParsedCar]) -> None:
    if not cars:
        return
    url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return
    try:
        from supabase import create_client
        from .db import save as db_save
        client = create_client(url, key)
        # Supabase client is sync — run in a thread to avoid blocking the loop.
        saved, updated, errors = await asyncio.to_thread(
            db_save, cars, client, "parser_search", None, None, _SEARCH_RESULT_TTL_HOURS,
        )
        logger.info(f"[db:search] persisted {saved} new + {updated} updated + {errors} errors")
    except Exception as e:
        # Background task — never let this crash the user-facing search.
        logger.warning(f"[db:search] persist failed: {e}")

# Model name normalization per site
# AS24 uses "3er", "4er", "c-klasse" — other sites need different names
# BMW uses "3er" on AS24 but other sites need "3 series" or "320"
# Swedish sites search by text — "BMW 3" works better than "BMW 3er"
# Model name conversion per site.
# AS24 uses "3er", "c-klasse" — Bytbil needs "320" or "C-Klass".
# Tested: Bytbil FreeText needs 2+ chars for model. Single digit "3"/"4"/"C" returns 0.

# For Swedish sites (Bytbil/Blocket) — need concrete sub-model or Klass name
MODEL_SWEDISH = {
    # BMW: use first popular sub-model number
    "1er": "120", "2er": "220", "3er": "320", "4er": "420",
    "5er": "520", "6er": "630", "7er": "730", "8er": "840",
    # Mercedes: use "-Klass" (Swedish) format
    "c-klasse": "C-Klass", "c-class": "C-Klass",
    "e-klasse": "E-Klass", "e-class": "E-Klass",
    "s-klasse": "S-Klass", "s-class": "S-Klass",
    "a-klasse": "A-Klass", "a-class": "A-Klass",
    "b-klasse": "B-Klass", "b-class": "B-Klass",
    # Others pass through (golf, v60, a6, octavia — already work)
}

def _swedish_model(model: str) -> str:
    """Model name for Bytbil/Blocket search."""
    return MODEL_SWEDISH.get(model.lower(), model)


# ══════════════════════════════════════════════════════════════════════════════
#  INDIVIDUAL SCRAPERS — each returns list[ParsedCar]
# ══════════════════════════════════════════════════════════════════════════════

async def _as24(params: dict, n: int) -> list[ParsedCar]:
    from .parser_autoscout24 import parse_http
    return await parse_http(params, max_results=n)


async def _bytbil(params: dict, n: int) -> list[ParsedCar]:
    from .parser_sweden import parse_bytbil
    rate = await get_sek_to_eur()
    model = _swedish_model(params.get("model", ""))
    return await parse_bytbil(
        None, rate,
        brand=params.get("brand", ""), model=model,
        year_from=params.get("year_from", 2018), year_to=params.get("year_to"),
        price_from_eur=params.get("price_from"),
        price_to_eur=params.get("price_to"), max_results=n,
        fuel=params.get("fuel"), transmission=params.get("transmission"),
        body_type=params.get("body_type"),
        vehicle_type=params.get("vehicle_type") or "Car",
    )


async def _blocket(params: dict, n: int) -> list[ParsedCar]:
    # Blocket migrated to a "/mobility" JSON search API that returns rich,
    # structured docs[] per car AND honours every filter server-side. The old
    # Playwright-list + httpx-ld+json-detail path targeted the pre-migration
    # Next.js site → it returned 0 for body/color/hp/drive and ignored
    # fuel/body/transmission filters. parse_blocket_api replaces both.
    rate = await get_sek_to_eur()
    from .parser_sweden import parse_blocket_api
    return await parse_blocket_api(
        rate,
        brand=params.get("brand", ""),
        model=_swedish_model(params.get("model", "")),
        year_from=params.get("year_from", 2018),
        year_to=params.get("year_to"),
        price_from_eur=params.get("price_from"),
        price_to_eur=params.get("price_to"),
        max_results=n,
        fuel=params.get("fuel"),
        transmission=params.get("transmission"),
        body_type=params.get("body_type"),
        drive=params.get("drive"),
        vehicle_type=params.get("vehicle_type") or "Car",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  DEDUPLICATION — single function, used everywhere
# ══════════════════════════════════════════════════════════════════════════════

def _model_family(model: str) -> str:
    """Normalize a model name to its family for peer-group comparison.
    "330 e xDrive Touring" → "3 series"
    "M3 Competition" → "3 series"
    "X5 M50i" → "x5"
    "C 200 AMG Line" → "c-class"
    "Q5 40 TDI" → "q5"
    "Passat Variant" → "passat"
    """
    m = (model or "").lower().strip()
    if not m:
        return ""
    # Audi Q-line: Q3/Q5/Q7/Q8 — single letter + digit, no space between
    mq = re.match(r'^q(\d)(?:\s|$)', m)
    if mq:
        return f"q{mq.group(1)}"
    # BMW X-line: X1/X3/X5/X6/X7 → "x5" etc.
    mx = re.match(r'^x(\d)(?:\s|$)', m)
    if mx:
        return f"x{mx.group(1)}"
    # BMW M-series: M3/M4/M5/M8 → map to respective series (M3 → 3 series)
    mm = re.match(r'^m(\d)(?:\s|$)', m)
    if mm:
        return f"{mm.group(1)} series"
    # BMW numeric series: 320, 330 → "3 series" (three-digit number)
    msr = re.match(r'^(\d)(\d{2})(?:\s|$|\D)', m)
    if msr and msr.group(1) in "123456789":
        return f"{msr.group(1)} series"
    # Mercedes letter-class: "C 200", "E 350", "S 500" — letter + SPACE + digit
    mc = re.match(r'^([a-z])\s+\d', m)
    if mc and mc.group(1) in "abceglsv":  # A/B/C/E/G/L/S/V classes
        return f"{mc.group(1)}-class"
    # Default: first token as family (Passat, Golf, Octavia)
    toks = m.split()
    return toks[0] if toks else m


def _hp_band(hp):
    """Bucket horsepower — cars in the same tier are genuine pricing peers.
    A 190 hp 320d and a 510 hp M3 are both '3 Series' but totally different cars —
    price-comparing them makes no sense. HP bands keep peers meaningful.
    """
    if not hp:
        return 0
    if hp < 150:   return 1   # eco (base diesels, small petrols)
    if hp < 250:   return 2   # standard (most 20d/30i variants)
    if hp < 350:   return 3   # sport (330e, 340i, Q5 45, XC60 T6)
    if hp < 450:   return 4   # high-performance (M340, S5, C43 AMG)
    return 5                  # super (M3/M4/M5, RS6, AMG 63)


def _body_band(body_type: str) -> str:
    """Canonical body class — Touring/Estate/Variant all map to 'estate'."""
    b = (body_type or "").lower().strip()
    if not b: return ""
    if b in ("sedan", "limousine", "saloon", "sedan/saloon"): return "sedan"
    if b in ("estate", "touring", "variant", "wagon", "kombi"): return "estate"
    if b in ("coupe", "coupé"): return "coupe"
    if b in ("convertible", "cabrio", "cabriolet", "roadster"): return "convertible"
    if b in ("suv", "crossover", "sav"): return "suv"
    if b in ("hatchback", "halvkombi"): return "hatch"
    if b in ("van", "minivan", "minibuss"): return "van"
    return b


def _apply_deal_bonus(cars: list[ParsedCar]) -> None:
    """Mutate each car's score with a bonus/penalty based on price vs peer-group median.

    Peer group = (make, model_family, year_bucket, hp_band, body_band).
    This keeps 320d sedan (190 hp) separate from M3 sedan (510 hp) and from 330e
    Touring (292 hp estate) — comparing like-for-like, not mashing all "3 Series"
    together. A "good deal" vs other M3s ≠ a "good deal" vs base diesels.

    Needs ≥3 cars in a group for tight comparison; otherwise relaxes step-by-step
    (drop body_band → drop hp_band) with halved bonus magnitudes (lower confidence).
    """
    from collections import defaultdict
    from statistics import median

    def _key_for(c):
        year_bucket = (c.year // 2) if c.year else 0
        return (
            c.make.lower(),
            _model_family(c.model or ""),
            year_bucket,
            _hp_band(c.horsepower),
            _body_band(c.body_type or ""),
        )

    # Pre-compute price lists at 3 key granularities for fallback.
    by_tight: dict[tuple, list[float]] = defaultdict(list)        # full 5-tuple
    by_no_body: dict[tuple, list[float]] = defaultdict(list)      # 4-tuple (no body)
    by_no_hp: dict[tuple, list[float]] = defaultdict(list)        # 3-tuple (no body+hp)
    for c in cars:
        if not c.price_eur or not c.make:
            continue
        k = _key_for(c)
        by_tight[k].append(c.price_eur)
        by_no_body[k[:4]].append(c.price_eur)
        by_no_hp[k[:3]].append(c.price_eur)

    for c in cars:
        if not c.price_eur or not c.make:
            continue
        k = _key_for(c)
        tier = None
        prices = by_tight[k]
        if len(prices) >= 3:
            tier = "tight"
        else:
            prices = by_no_body[k[:4]]
            if len(prices) >= 3:
                tier = "relaxed-body"
            else:
                prices = by_no_hp[k[:3]]
                if len(prices) >= 3:
                    tier = "relaxed-hp"
        if tier is None:
            continue  # still too sparse — leave score alone
        med = median(prices)
        if med <= 0:
            continue
        ratio = c.price_eur / med
        if tier == "tight":
            # High confidence — full ±15 range
            if ratio < 0.80:   bonus = 15
            elif ratio < 0.90: bonus = 10
            elif ratio < 0.95: bonus = 5
            elif ratio < 1.05: bonus = 0
            elif ratio < 1.15: bonus = -5
            elif ratio < 1.30: bonus = -10
            else:              bonus = -15
        else:
            # Lower confidence — halved effect to avoid over-penalizing different classes
            if ratio < 0.80:   bonus = 7
            elif ratio < 0.90: bonus = 4
            elif ratio < 1.10: bonus = 0
            elif ratio < 1.30: bonus = -4
            else:              bonus = -7
        c.score = max(0, min(100, c.score + bonus))


def deduplicate(cars: list[ParsedCar]) -> list[ParsedCar]:
    """Cross-source dedup: same car on AS24 + Bytbil = 1 result (keep richest). O(n)."""
    seen_urls: set[str] = set()
    by_content: dict[str, ParsedCar] = {}  # content_key → best car
    no_key: list[ParsedCar] = []  # cars without mileage+price (can't dedup by content)

    def richness(c: ParsedCar) -> int:
        n = len(c.gallery or [])
        n += len(c.safety_features or []) + len(c.comfort_features or []) + len(c.infotainment or [])
        if c.color: n += 1
        if c.drive: n += 1
        if c.horsepower: n += 1
        return n

    for car in cars:
        if car.source_url in seen_urls:
            continue
        seen_urls.add(car.source_url)

        if car.mileage_km and car.price_eur:
            # Collapse same car across sources: normalize model to family level
            # (330e xDrive Touring → "3 series") and bucket price/km to reduce
            # noise. Previous: rounded to €10 (useless).
            price_bucket = int(round(car.price_eur / 500) * 500)
            km_bucket = int(round(car.mileage_km / 1000) * 1000)
            family = _model_family(car.model or "")
            key = f"{(car.make or '').lower()}|{family}|{car.year}|{km_bucket}|{price_bucket}"
        elif car.source_site and car.external_id:
            # Fallback: dedup by external_id when mileage/price missing
            key = f"{car.source_site}|{car.external_id}"
        else:
            no_key.append(car)
            continue

        existing = by_content.get(key)
        if existing:
            if richness(car) > richness(existing):
                by_content[key] = car
        else:
            by_content[key] = car

    return list(by_content.values()) + no_key


# ══════════════════════════════════════════════════════════════════════════════
#  PARALLEL SCRAPE — all 4 sites at once
# ══════════════════════════════════════════════════════════════════════════════

SOURCES = {
    "autoscout24": _as24,
    "bytbil": _bytbil,
    "blocket": _blocket,
}


SCRAPE_DEADLINE_S = 14.0  # Hard wall-clock budget per cold scrape
EARLY_RETURN_THRESHOLD = 80  # Once we have this many unique cars, stop waiting


async def _scrape_all(params: dict, per_source: int = 20) -> list[ParsedCar]:
    """Tiered parallel scrape with early termination.

    Tier 1 (always): AS24 + Bytbil — fast HTTP, reliable.
    Tier 2 (only when brand is set): Blocket — slow Playwright, only useful with brand.
        For body-only / no-brand queries, Blocket free-text returns garbage and
        slows us down without adding quality.

    Returns as soon as: all tasks done, OR ≥80 unique cars, OR 14s deadline.
    Pending tasks are cancelled.
    """
    from . import metrics
    import time as _t

    async def _run(name: str, fn):
        t0 = _t.monotonic()
        try:
            result = await fn(params, per_source)
            elapsed_ms = (_t.monotonic() - t0) * 1000
            count = len(result) if result else 0
            kind = "success" if count > 0 else "empty"
            metrics.record(name, kind, elapsed_ms)
            return name, result
        except Exception as e:
            elapsed_ms = (_t.monotonic() - t0) * 1000
            kind = metrics.classify_error(exc=e)
            metrics.record(name, kind, elapsed_ms, error=str(e))
            return name, e

    # Decide tier set
    has_brand = bool((params.get("brand") or "").strip())
    active_sources = ["autoscout24", "bytbil"]
    if has_brand and "blocket" in SOURCES:
        active_sources.append("blocket")

    tasks = {}
    for name in active_sources:
        if name not in SOURCES:
            continue
        breaker = get_breaker(name)
        tasks[name] = asyncio.create_task(breaker.call(_run(name, SOURCES[name])))

    all_cars: list[ParsedCar] = []
    done_names: set[str] = set()
    deadline = _t.monotonic() + SCRAPE_DEADLINE_S

    pending = set(tasks.values())
    while pending:
        timeout = max(0.1, deadline - _t.monotonic())
        try:
            done, pending = await asyncio.wait(
                pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            for t in pending:
                t.cancel()
            raise

        if not done:
            # Hit deadline
            logger.warning(f"[scrape] deadline {SCRAPE_DEADLINE_S}s hit, "
                           f"cancelling {len(pending)} pending source(s)")
            for t in pending:
                t.cancel()
            break

        for task in done:
            try:
                result = task.result()
                if isinstance(result, tuple):
                    name, payload = result
                    done_names.add(name)
                    if isinstance(payload, Exception):
                        logger.error(f"[{name}] {payload}")
                    elif payload:
                        all_cars.extend(payload)
                        logger.info(f"[{name}] {len(payload)} cars")
                    else:
                        logger.info(f"[{name}] 0 cars")
            except (asyncio.CancelledError, Exception) as e:
                logger.error(f"[scrape] task error: {e}")

        # Early-return if we have enough
        if len(all_cars) >= EARLY_RETURN_THRESHOLD and pending:
            logger.info(f"[scrape] {len(all_cars)} cars reached, "
                        f"cancelling {len(pending)} pending source(s)")
            for t in pending:
                t.cancel()
            break

    for name in active_sources:
        if name not in done_names:
            logger.info(f"[{name}] (skipped/cancelled)")

    # Hard brand filter — some parsers (Blocket free-text, AS24 fuzzy) leak wrong makes
    wanted_brand = (params.get("brand") or "").strip().lower()
    if wanted_brand:
        before = len(all_cars)
        all_cars = [
            c for c in all_cars
            if not c.make or wanted_brand in (c.make or "").lower() or (c.make or "").lower() in wanted_brand
        ]
        if len(all_cars) != before:
            logger.info(f"[filter] brand {before} → {len(all_cars)} (wanted={wanted_brand})")

    # Hard model filter — AS24 URL slugs for BMW /3-series, MB /c-class etc. silently
    # return ALL make results (AS24 routing quirk). Post-filter by model tokens.
    wanted_model = (params.get("model") or "").strip().lower()
    if wanted_model:
        # Extract significant tokens from requested model.
        # "3 series" → {3, series}; "X5" → {x5}; "c-class" → {c, class}
        req_tokens = {t for t in re.split(r'[\s\-]+', wanted_model) if t and t != "series" and t != "class" and t != "klasse"}
        # Sub-model numbers: "3 series" → accept models starting with 3 (320, 330, 335, M3).
        # Exception: "3 series" must NOT match "X3" (separate SUV line) nor "330 i" etc only if first digit is 3.
        # Performance-trim tokens: when the request names one of these, a base
        # model that lacks it (plain "X5" for "X5 M", "Golf" for "Golf GTI") must
        # NOT match — the loose substring/token-overlap below otherwise floods the
        # result with the wrong, often over-budget, base variant.
        # ONLY tokens that sources reliably write into the `model` field belong
        # here. Single-letter "n"/"r"/"s" are EXCLUDED: sources leave them in the
        # spec/title, not `model` (e.g. Hyundai i30 N is stored model="I30"), so
        # gating on them would over-filter every real trim to ZERO. "m" stays —
        # BMW X-M cars are reliably labelled "X5 M"/"X3 M". Those un-gated trims
        # (i30 N, Golf R, AMG sub-specs on Blocket) need the source-level model/
        # motor codes (as24_motors / Blocket variant) to be targeted precisely.
        _PERF_TRIMS = {"gti", "gtd", "amg", "rs", "gts", "competition", "jcw",
                       "cupra", "abarth", "quadrifoglio", "m"}
        wanted_trims = req_tokens & _PERF_TRIMS

        def _model_matches(c_model: str) -> bool:
            cm = (c_model or "").lower().strip()
            if not cm:
                return True  # unknown model → don't drop
            # Trim gate (runs FIRST so it overrides the permissive matches below):
            # the car must carry EVERY requested trim token AND share the base
            # model token — so "X5 M" rejects plain "X5" (no trim) and "X4 M"
            # (trim ok, wrong base), and "Golf GTI" rejects "Polo GTI".
            if wanted_trims:
                cm_tok = {t for t in re.split(r'[\s\-]+', cm) if t}
                if not wanted_trims.issubset(cm_tok):
                    return False
                base_tokens = req_tokens - wanted_trims
                if base_tokens and not (base_tokens & cm_tok):
                    return False  # trim matches but base model differs
                return True
            if wanted_model in cm or cm in wanted_model:
                return True
            # Series match: "3 series" user wants → accept 3xx numbers (320, 330, M3 340e etc)
            # but NOT X-line (X1, X3, X5, X7) nor other series (4xx, 5xx, 7xx, 8xx).
            if "series" in wanted_model or wanted_model.isdigit() or re.match(r'^\d+(er)?$', wanted_model):
                # Extract the digit: "3 series" → "3"; "3er" → "3"
                dm = re.match(r'^(\d)', wanted_model)
                if dm:
                    series_digit = dm.group(1)
                    # Accept if car model starts with that digit AND is not X-series
                    if cm.startswith("x"):
                        return False
                    cm_digit_match = re.match(r'^[mM]?(\d)', cm)
                    if cm_digit_match and cm_digit_match.group(1) == series_digit:
                        return True
                    return False
            # Class match: "c-class"/"e-klasse" → accept models starting with that letter
            # followed by digit/space/hyphen (compact "C220", separated "C 220", named "C-Class").
            # Reject CLA/CLS/CLK/Cabriolet (letter followed by another letter).
            if "class" in wanted_model or "klasse" in wanted_model:
                cm_letter = re.match(r'^([a-zA-Z])[\s\-]', wanted_model)
                if cm_letter:
                    letter = cm_letter.group(1).lower()
                    if cm == letter:
                        return True
                    if cm.startswith(letter):
                        nxt = cm[1:2]
                        # Accept if next char is digit, space, or hyphen
                        if nxt.isdigit() or nxt in (" ", "-"):
                            return True
                    return False
            # Default: require at least one significant token overlap
            cm_tokens = {t for t in re.split(r'[\s\-]+', cm) if t}
            return bool(req_tokens & cm_tokens)

        before = len(all_cars)
        all_cars = [c for c in all_cars if _model_matches(c.model or "")]
        if len(all_cars) != before:
            logger.info(f"[filter] model {before} → {len(all_cars)} (wanted={wanted_model})")

    # ── Quality filter — remove clearly bad listings ──────────────────────────
    # 1. Price sanity: <€3 000 or >€500 000 → broken data (currency typo, scam)
    # 2. Explicit damage (has_damage==True) — drop
    # 3. 4+ previous owners — usually problematic history
    # 4. Suspicious mileage (clocked odometer heuristic)
    from datetime import datetime as _dt
    before_q = len(all_cars)
    filtered: list[ParsedCar] = []
    dropped = {"price": 0, "damage": 0, "owners": 0, "mileage": 0}
    for c in all_cars:
        if c.price_eur is not None and (c.price_eur < 3000 or c.price_eur > 500_000):
            dropped["price"] += 1
            continue
        if c.has_damage is True:
            dropped["damage"] += 1
            continue
        if c.previous_owners is not None and c.previous_owners >= 5:
            dropped["owners"] += 1
            continue
        # Suspiciously low km for age (likely odometer rollback)
        if c.year and c.mileage_km and c.mileage_km > 0:
            age = max(1, _dt.now().year - c.year)
            if age >= 4 and (c.mileage_km / age) < 3000:
                dropped["mileage"] += 1
                continue
        filtered.append(c)
    if sum(dropped.values()) > 0:
        logger.info(f"[filter:quality] {before_q} → {len(filtered)} dropped={dropped}")
    all_cars = filtered

    unique = deduplicate(all_cars)

    # ── Deal score vs in-search market ────────────────────────────────────────
    # Compute median price per (make, model-family) in the current result set.
    # A car priced below median for its peer group gets a deal bonus, above gets penalty.
    # This works without an external price guide — uses the current scrape as reference.
    _apply_deal_bonus(unique)

    unique.sort(key=lambda c: c.score, reverse=True)

    # Learn: record prices into price_memory (async, don't block)
    if unique:
        try:
            from .price_memory import record_prices
            record_prices(unique, source="search")
        except Exception:
            pass  # never block search for analytics

    # Background DB save — every cold scrape persists its cars so they show
    # up on the site's /order page alongside the hot-deals batch.
    # Detached: won't delay the search response.
    if unique:
        try:
            asyncio.create_task(_persist_search_results(unique))
        except Exception:
            pass

    return unique


# ══════════════════════════════════════════════════════════════════════════════
#  POST-CACHE USER FILTERS
# ══════════════════════════════════════════════════════════════════════════════
#
# Strict filters (color/drive/fuel/transmission/body_type/doors/seats/hp_min)
# run AFTER cache retrieval, not inside _scrape_all. This way the cache stores
# raw scraped+quality-filtered results, and the same cached set serves different
# user-filter combinations cheaply.
#
# NULL handling: differs by source.
#   • Bytbil: reliable BodyTypes=*/FuelTypes=* URL filters → trust NULL.
#   • AS24: &body=N URL filter works (codes verified empirically). Some leakage
#     remains (~10% wrong body), so model-name detection in parser catches obvious
#     mismatches. Trust NULL conservatively.
#   • Blocket: only free-text search, no native filters → drop NULL.
# Drive: NO source extracts drive reliably from list pages — trust NULL from all.

# Bytbil + AS24. AS24's URL filter is leaky (~30%), but our parser now uses
# MODEL_BODY hardcoded inference (base.py:infer_body_from_model) as fallback,
# so most popular models get body_type extracted correctly. Cars where
# inference fails (rare/exotic models) trust AS24's URL hint.
_SOURCES_WITH_BODY_FILTER = {"bytbil.com", "autoscout24.com"}
_SOURCES_WITH_FUEL_FILTER = {"autoscout24.com", "bytbil.com"}
_SOURCES_WITH_TRANS_FILTER = {"autoscout24.com", "bytbil.com"}
_ALL_SOURCES = {"autoscout24.com", "bytbil.com", "blocket.se"}


def _apply_user_filters(cars: list[ParsedCar], params: dict) -> list[ParsedCar]:
    # Color/drive/transmission: parsers don't reliably extract these from list
    # pages, so we trust NULL from all sources (treat user input as soft preference).
    # AS24/Bytbil DO apply transmission URL filter (&gear=*/&Gearboxes=*) so trust is safe.
    strict_filters = [
        ("color", lambda c: c.color, _ALL_SOURCES),
        ("drive", lambda c: c.drive, _ALL_SOURCES),
        ("fuel", lambda c: c.fuel, _SOURCES_WITH_FUEL_FILTER),
        ("transmission", lambda c: c.transmission, _SOURCES_WITH_TRANS_FILTER),
        ("body_type", lambda c: c.body_type, _SOURCES_WITH_BODY_FILTER),
    ]
    for field_name, getter, trust_sources in strict_filters:
        wanted = (params.get(field_name) or "").strip()
        if not wanted:
            continue
        wanted_set = {w.strip().lower() for w in wanted.split(",") if w.strip()}
        if not wanted_set:
            continue
        before = len(cars)

        def _keep(c, getter=getter, wanted_set=wanted_set, trust=trust_sources) -> bool:
            v = getter(c)
            if v:
                return v.lower() in wanted_set
            return c.source_site in trust

        cars = [c for c in cars if _keep(c)]
        if len(cars) != before:
            logger.info(f"[filter:strict] {field_name}={wanted}: {before} → {len(cars)}")

    # Numeric filters
    doors_req = params.get("doors")
    if doors_req:
        try:
            dr = int(doors_req)
            before = len(cars)
            cars = [c for c in cars if not c.doors or c.doors == dr]
            if len(cars) != before:
                logger.info(f"[filter:strict] doors={dr}: {before} → {len(cars)}")
        except (ValueError, TypeError):
            pass
    seats_min = params.get("seats_min") or params.get("seats")
    if seats_min:
        try:
            sm = int(seats_min)
            before = len(cars)
            cars = [c for c in cars if not c.seats or c.seats >= sm]
            if len(cars) != before:
                logger.info(f"[filter:strict] seats>={sm}: {before} → {len(cars)}")
        except (ValueError, TypeError):
            pass
    hp_min = params.get("hp_min")
    if hp_min:
        try:
            hm = int(hp_min)
            before = len(cars)
            cars = [c for c in cars if not c.horsepower or c.horsepower >= hm]
            if len(cars) != before:
                logger.info(f"[filter:strict] hp>={hm}: {before} → {len(cars)}")
        except (ValueError, TypeError):
            pass
    return cars


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

async def search(params: dict, max_results: int = DEFAULT_MAX_RESULTS) -> list[ParsedCar]:
    """
    THE search function. Cache-on-read + singleflight + parallel scrape.

    Cache hit: <5ms
    Cold miss: 2-5s (parallel 4 sites)
    Singleflight: N concurrent identical searches → 1 scrape
    """
    key = cache_key(
        params.get("brand", ""),
        params.get("model", ""),
        vehicle_type=params.get("vehicle_type") or "Car",
        fuel=params.get("fuel"),
        year_from=params.get("year_from"),
        year_to=params.get("year_to"),
        price_from=params.get("price_from"),
        price_to=params.get("price_to"),
        body_type=params.get("body_type"),
        transmission=params.get("transmission"),
    )
    # Always scrape 50 per source (~150 total) regardless of requested limit.
    # Cache stores full results; limit applied at return time.
    PER_SOURCE = 50

    results = await get_or_scrape(
        key=key,
        scrape=lambda: _scrape_all(params, PER_SOURCE),
    )

    # Post-cache price filter — cache key doesn't include price_from, so filter here
    price_from = params.get("price_from")
    price_to = params.get("price_to")
    if price_from or price_to:
        results = [
            c for c in results
            if c.price_eur is None
            or ((not price_from or c.price_eur >= price_from)
                and (not price_to or c.price_eur <= price_to))
        ]

    # Post-cache year filter — AS24's &fregto= URL filter occasionally leaks
    # newer cars (e.g. 2024 in a year_to=2023 query). Enforce strictly here.
    # When client asked for 2018+ (modern), DROP cars with year=None — Blocket
    # listings of old beaters (2003-2010 Honda Accord etc.) routinely arrive
    # without regdate and were polluting the result set for modern queries.
    year_from = params.get("year_from")
    year_to = params.get("year_to")
    if year_from or year_to:
        strict_unknown_year = bool(year_from and year_from >= 2018)
        def _year_ok(c):
            if c.year is None:
                return not strict_unknown_year
            if year_from and c.year < year_from:
                return False
            if year_to and c.year > year_to:
                return False
            return True
        results = [c for c in results if _year_ok(c)]

    # Post-cache user-strict filters (body_type/fuel/transmission/drive/color/etc.)
    # Same cached set serves any combination of these without re-scraping.
    results = _apply_user_filters(results, params)

    return results[:max_results]


async def search_stream(
    params: dict, per_source: int = 50,
) -> AsyncIterator[tuple[str, list[ParsedCar]]]:
    """Progressive SSE search:

    1. Cache check → if hit, emit cached cars immediately as ('cache', cars)
       AND skip sources (cache is fresh).
    2. Otherwise: fire AS24 + Bytbil in parallel, Blocket only if brand set.
       Each source yields ('source_name', cars) when it completes.
    3. Apply user filters per batch so user sees correctly-filtered results
       progressively, not raw scraped cars.
    """
    from .cache import cache_key as _ck, _store
    import time as _t

    # Same cache key format as search()
    key = _ck(
        params.get("brand", ""),
        params.get("model", ""),
        vehicle_type=params.get("vehicle_type") or "Car",
        fuel=params.get("fuel"),
        year_from=params.get("year_from"),
        year_to=params.get("year_to"),
        price_from=params.get("price_from"),
        price_to=params.get("price_to"),
        body_type=params.get("body_type"),
        transmission=params.get("transmission"),
    )

    # Cache hit — emit immediately, no scrape
    entry = _store.get(key)
    if entry is not None:
        cached_cars, ts = entry
        # Apply same post-cache filters as search()
        results = list(cached_cars)
        price_from = params.get("price_from")
        price_to = params.get("price_to")
        if price_from or price_to:
            results = [
                c for c in results
                if c.price_eur is None
                or ((not price_from or c.price_eur >= price_from)
                    and (not price_to or c.price_eur <= price_to))
            ]
        year_from = params.get("year_from")
        year_to = params.get("year_to")
        if year_from or year_to:
            strict_unknown_year_2 = bool(year_from and year_from >= 2018)
            def _year_ok_2(c):
                if c.year is None:
                    return not strict_unknown_year_2
                if year_from and c.year < year_from:
                    return False
                if year_to and c.year > year_to:
                    return False
                return True
            results = [c for c in results if _year_ok_2(c)]
        results = _apply_user_filters(results, params)
        if results:
            yield "cache", results
        # Stale cache → also rescrape in background to populate next user
        if _t.monotonic() - ts < 3600:  # under 1h: trust cache, don't rescrape
            return
        # else: fall through to rescrape

    # Cold path — stream from sources
    has_brand = bool((params.get("brand") or "").strip())
    active = ["autoscout24", "bytbil"]
    if has_brand and "blocket" in SOURCES:
        active.append("blocket")

    all_cars: list[ParsedCar] = []
    tasks: dict[str, asyncio.Task] = {}
    for name in active:
        if name not in SOURCES:
            continue
        breaker = get_breaker(name)
        fn = SOURCES[name]

        async def _run(n=name, f=fn):
            try:
                result = await breaker.call(f(params, per_source))
                return n, result or []
            except Exception as e:
                logger.error(f"[stream:{n}] {e}")
                return n, []

        tasks[name] = asyncio.create_task(_run())

    pending = set(tasks.values())
    deadline = _t.monotonic() + SCRAPE_DEADLINE_S

    while pending:
        timeout = max(0.1, deadline - _t.monotonic())
        done, pending = await asyncio.wait(
            pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            for t in pending:
                t.cancel()
            break
        for task in done:
            try:
                name, cars = task.result()
                if not cars:
                    logger.info(f"[stream:{name}] 0 cars")
                    continue
                # Apply user filters per batch (so user sees only matching cars)
                filtered = _apply_user_filters(cars, params)
                # Apply price/year post-filter
                pf = params.get("price_from"); pt = params.get("price_to")
                yf = params.get("year_from"); yt = params.get("year_to")
                if pf or pt:
                    filtered = [c for c in filtered if c.price_eur is None
                                or ((not pf or c.price_eur >= pf) and (not pt or c.price_eur <= pt))]
                if yf or yt:
                    filtered = [c for c in filtered if c.year is None
                                or ((not yf or c.year >= yf) and (not yt or c.year <= yt))]
                all_cars.extend(cars)  # cache the unfiltered batch
                logger.info(f"[stream:{name}] {len(filtered)}/{len(cars)} cars after filter")
                if filtered:
                    yield name, filtered
            except Exception as e:
                logger.error(f"[stream] task error: {e}")

    # Cache the union of all scraped cars for next user
    if all_cars:
        try:
            unique = deduplicate(all_cars)
            _store[key] = (unique, _t.monotonic())
        except Exception:
            pass
