"""
Fresh Auto Parser API v5.0
Cache-on-Read + Singleflight + Parallel Scraping + SSE Streaming.

Endpoints:
  GET  /search/instant     — Smart search: cache → singleflight → parallel scrape
  GET  /search/stream      — SSE: results stream as each source completes
  POST /search             — Legacy async search with job queue (backward compat)
  POST /search/sync        — Sync search (testing only)
  GET  /job/{id}           — Poll job status + results
  POST /hot                — Trigger hot deals refresh
  GET  /health             — Health + browser pool + cache stats
  GET  /stats              — Cache + circuit breaker stats
"""

import asyncio
import os
import sys
import json
from typing import Optional
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from supabase import create_client
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
API_KEY = os.getenv("PARSER_API_KEY", "")


def _validate_env():
    """Fail fast at startup if required env vars missing."""
    missing = []
    if not SUPABASE_URL: missing.append("SUPABASE_URL")
    if not SUPABASE_SERVICE_KEY: missing.append("SUPABASE_SERVICE_KEY")
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    # Warn on optional but recommended
    if not os.getenv("MOBILEDE_USER") or not os.getenv("MOBILEDE_PASS"):
        logger.warning("[env] MOBILEDE_USER/MOBILEDE_PASS not set — Mobile.de search will return empty")
    if not API_KEY:
        logger.warning("[env] PARSER_API_KEY not set — API auth disabled (anyone can query)")


# ═══════════════════════════════════════════════════════════════════════════════
#  APP SETUP
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app):
    # Install structured error sink BEFORE validate_env so even startup failures are captured.
    from parsers._logging_sink import install as _install_logs
    _install_logs()
    _validate_env()
    logger.info("[api] v5.0 starting")
    # Pre-warm the Playwright/Patchright browser so the first real request doesn't
    # pay the 5-10s cold-start penalty. Non-blocking fallback if it fails.
    try:
        from parsers.browser_pool import BrowserPool
        await BrowserPool.prewarm()
    except Exception as e:
        logger.warning(f"[api] browser prewarm failed (non-fatal): {e}")
    yield
    from parsers.browser_pool import BrowserPool
    await BrowserPool.shutdown()
    logger.info("[api] shutdown")

app = FastAPI(title="Fresh Auto Parser", version="5.0.0", lifespan=lifespan)

# CORS: whitelist specific origins. Wildcard "*" is unsafe for auth'd endpoints —
# respect PARSER_ALLOWED_ORIGINS env (comma-separated) or fall back to our site.
_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv(
        "PARSER_ALLOWED_ORIGINS",
        "http://localhost:3001,http://localhost:3000,https://freshauto.com.ua",
    ).split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)


# ── Correlation-ID middleware ────────────────────────────────────────────────
# Each incoming request gets a short ID (client can send one via X-Request-ID
# or we generate one). It's bound to loguru context so every log line for this
# request carries it — makes multi-step traces (cache → scrape → parse) legible.

import uuid
from starlette.middleware.base import BaseHTTPMiddleware


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:10]
        with logger.contextualize(rid=rid):
            response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


app.add_middleware(CorrelationIDMiddleware)


def _auth(key: Optional[str]):
    if API_KEY and key != API_KEY:
        raise HTTPException(401, "Unauthorized")

def _supabase():
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

import re as _re

def _clean_str(v):
    """Remove control characters that break JSON serialization."""
    if isinstance(v, str):
        # Remove all control chars except newline
        return _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', v)
    return v

def _serialize(item) -> dict:
    if hasattr(item, "__dataclass_fields__"):
        d = asdict(item)
        d["price"] = d.pop("price_eur", None)
        d["mileage"] = d.pop("mileage_km", None)
        # Keep score (useful for client-side debug / sorting) — don't strip.
        for k in ["price_original", "price_currency", "external_id", "location"]:
            d.pop(k, None)
        d["features_ua"] = d.pop("features_other", [])
        if not d.get("image") and d.get("gallery"):
            d["image"] = d["gallery"][0]
        # Clean control characters from string fields
        for k, v in d.items():
            if isinstance(v, str):
                d[k] = _clean_str(v)
            elif isinstance(v, list):
                d[k] = [_clean_str(x) if isinstance(x, str) else x for x in v]
        return d
    return item if isinstance(item, dict) else {}


class SearchRequest(BaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None
    fuel: Optional[str] = None
    transmission: Optional[str] = None
    body_type: Optional[str] = None
    drive: Optional[str] = None
    client_order_id: Optional[str] = None
    max_results: Optional[int] = 20
    skip_cache: Optional[bool] = False


def _params(req: SearchRequest) -> dict:
    return {
        "brand": req.make or "", "model": req.model or "",
        "year_from": req.year_from or 2018, "year_to": req.year_to,
        "price_from": req.budget_min, "price_to": req.budget_max,
        "fuel": req.fuel, "transmission": req.transmission,
        "body_type": req.body_type, "drive": req.drive,
        "max_results": req.max_results or 20,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    from parsers.browser_pool import BrowserPool
    from parsers.cache import stats
    return {
        "status": "ok", "version": "5.0.0",
        "browser": "active" if BrowserPool.is_active() else "dormant",
        "cache": stats(),
    }


@app.get("/stats")
async def get_stats(x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    from parsers.cache import stats
    from parsers.resilience import all_statuses
    from parsers.metrics import snapshot
    return {"cache": stats(), "circuit": all_statuses(), "scrapers": snapshot()}


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus-compatible metrics exposition — no auth (standard pattern)."""
    from parsers.metrics import prometheus_format
    return PlainTextResponse(prometheus_format())


@app.get("/price-guide")
async def price_guide(x_api_key: Optional[str] = Header(None)):
    """Price memory — learned market prices from all searches."""
    _auth(x_api_key)
    try:
        from parsers.price_memory import get_price_guide
        guide = get_price_guide()
        return {"count": len(guide), "models": guide}
    except Exception as e:
        return {"count": 0, "models": {}, "note": "price_memory table not created yet"}


# ── INSTANT SEARCH (PRIMARY) ──────────────────────────────────────────────────

@app.get("/search/instant")
async def search_instant(
    make: str = "", model: str = "",
    vehicle_type: str = "Car",  # Car | Motorcycle | Truck | Van | Bus | Camper
    year_from: Optional[int] = None, year_to: Optional[int] = None,
    price_min: int = 5000, price_max: Optional[int] = None,
    fuel: Optional[str] = None, transmission: Optional[str] = None,
    body_type: Optional[str] = None, drive: Optional[str] = None,
    color: Optional[str] = None,
    sort: str = "score",
    limit: int = 30,
    x_api_key: Optional[str] = Header(None),
):
    """
    Smart search: cache → singleflight → parallel 4-site scrape.
    Cache hit: <5ms. Cold miss: 2-5s.
    Pass vehicle_type=Motorcycle to hit AS24 /motorrad/, etc.
    """
    _auth(x_api_key)
    params = {
        "brand": make, "model": model,
        "vehicle_type": vehicle_type,
        "year_from": year_from, "year_to": year_to,
        "price_from": price_min, "price_to": price_max,
        "fuel": fuel, "transmission": transmission,
        "body_type": body_type, "drive": drive,
        "color": color,
    }
    from parsers.orchestrator import search
    from starlette.responses import Response
    cars = await search(params, max_results=limit)
    serialized = [_serialize(c) for c in cars]
    # Manual JSON: encode + strip control chars to avoid serialization crashes
    body = json.dumps({"status": "ok", "count": len(serialized), "cars": serialized},
                      ensure_ascii=True, default=str)
    return Response(content=body, media_type="application/json")


# ── SSE STREAMING ─────────────────────────────────────────────────────────────

@app.get("/search/stream")
async def search_stream(
    make: str = "", model: str = "",
    vehicle_type: str = "Car",
    year_from: Optional[int] = None, year_to: Optional[int] = None,
    price_min: int = 5000, price_max: Optional[int] = None,
    fuel: Optional[str] = None, transmission: Optional[str] = None,
    body_type: Optional[str] = None, drive: Optional[str] = None,
    color: Optional[str] = None,
    limit: int = 50,
    x_api_key: Optional[str] = Header(None),
):
    """SSE streaming search with full filter support.
    Events:
      data: {"source": "cache", "cars": [...], "count": N}    — instant cache hit
      data: {"source": "autoscout24", "cars": [...], "count": N}
      data: {"source": "bytbil", "cars": [...], "count": N}
      data: {"done": true, "total": N}
    Each source emits results as it completes. Cache hit emits immediately.
    """
    _auth(x_api_key)
    params = {
        "brand": make, "model": model,
        "vehicle_type": vehicle_type,
        "year_from": year_from, "year_to": year_to,
        "price_from": price_min, "price_to": price_max,
        "fuel": fuel, "transmission": transmission,
        "body_type": body_type, "drive": drive,
        "color": color,
    }

    async def events():
        from parsers.orchestrator import search_stream as _stream
        total = 0
        seen_urls: set = set()
        try:
            async for source, cars in _stream(params, per_source=limit):
                # Dedupe against already-emitted cars (cache + sources can overlap)
                fresh = []
                for c in cars:
                    url = getattr(c, "source_url", None)
                    if url and url in seen_urls:
                        continue
                    if url:
                        seen_urls.add(url)
                    fresh.append(c)
                if not fresh:
                    continue
                data = [_serialize(c) for c in fresh]
                total += len(data)
                yield f"data: {json.dumps({'source': source, 'count': len(data), 'total': total, 'cars': data}, default=str, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"[stream] error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield f"data: {json.dumps({'done': True, 'total': total})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })


# ── LEGACY POST /search (backward compatible) ────────────────────────────────

@app.post("/search")
async def search_post(req: SearchRequest, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    params = _params(req)

    # Try smart search first
    if not req.skip_cache:
        try:
            from parsers.orchestrator import search
            cars = await search(params, max_results=req.max_results or 20)
            if cars and len(cars) >= 3:
                return {"status": "ok", "count": len(cars), "cars": [_serialize(c) for c in cars], "job_id": None}
        except Exception as e:
            logger.warning(f"[api] Smart search failed: {e}")

    # Fallback: DB cache
    try:
        supabase = _supabase()
        cached = _db_cache(supabase, params)
        if cached:
            return {"status": "cached", "count": len(cached), "cars": cached, "job_id": None}
    except Exception:
        pass

    # Fallback: background job
    try:
        from parsers.main import run_custom_search
        results = await run_custom_search(params=params, client_order_id=req.client_order_id)
        cars = [_serialize(r) for r in (results or [])]
        return {"status": "ok", "count": len(cars), "cars": cars, "job_id": None}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/search/sync")
async def search_sync(req: SearchRequest, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    from parsers.main import run_custom_search
    results = await run_custom_search(params=_params(req), client_order_id=req.client_order_id)
    cars = [_serialize(r) for r in (results or [])]
    return {"status": "ok", "count": len(cars), "cars": cars}


# ── JOB STATUS ────────────────────────────────────────────────────────────────

@app.get("/job/{job_id}")
async def get_job(job_id: str, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    supabase = _supabase()
    result = supabase.table("search_jobs").select("*").eq("id", job_id).execute()
    if not result.data:
        raise HTTPException(404, "Job not found")
    job = result.data[0]
    cars = []
    if job["status"] == "done" and job.get("client_order_id"):
        cars = (supabase.table("cars").select("*").eq("client_order_id", job["client_order_id"]).execute()).data or []
    return {
        "status": job["status"], "results_count": job.get("results_count", 0),
        "error": job.get("error"), "cars": cars,
    }


# ── HOT DEALS ─────────────────────────────────────────────────────────────────

@app.post("/hot")
async def hot(x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    from parsers.main import run_hot_deals
    results = await run_hot_deals()
    return {"status": "ok", "count": len(results) if results else 0}


# ── DB CACHE (legacy) ─────────────────────────────────────────────────────────

def _db_cache(supabase, params: dict, max_age_hours: int = 6) -> Optional[list]:
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        q = supabase.table("cars").select("*").gt("parsed_at", cutoff).in_("source_type", ["parser_featured", "parser_hot", "stock"])
        brand = (params.get("brand") or "").strip()
        model = (params.get("model") or "").strip()
        if brand: q = q.ilike("make", f"%{brand}%")
        if model: q = q.ilike("model", f"%{model}%")
        if params.get("year_from"): q = q.gte("year", params["year_from"])
        if params.get("price_to"): q = q.lte("price", params["price_to"])
        q = q.gte("price", max(params.get("price_from") or 0, 20000))
        result = q.limit(params.get("max_results", 20)).execute()
        return result.data if result.data and len(result.data) >= 3 else None
    except Exception:
        return None
