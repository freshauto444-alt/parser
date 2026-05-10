# Fresh Auto Parser v5.0

## Architecture: Cache-on-Read + Parallel Scraping

```
User search → cache hit? → <5ms return
                 ↓ miss
        Singleflight (10 users = 1 scrape)
                 ↓
        4 sites in parallel:
        ├── AS24:    curl_cffi + __NEXT_DATA__ (1 req = 20 cars)
        ├── Bytbil:  httpx (pure HTTP)
        ├── Blocket: blocket-api or Playwright fallback
        └── Mobile:  REST API (sandbox)
                 ↓
        Deduplicate → cache 2h → return
```

## Files

```
parsers/
├── api_server.py        # FastAPI: /search/instant, /search/stream (SSE), /search, /hot
├── orchestrator.py      # THE search engine: cache + singleflight + parallel scrape
├── cache.py             # L1 memory cache + singleflight + stale-while-revalidate
├── base.py              # ParsedCar dataclass + normalization + 90+ Swedish translations
├── db.py                # Supabase upsert/cleanup
├── main.py              # CLI: hot, search, worker
├── config.py            # ALL constants (rates, limits, models)
├── as24_http.py         # AS24: curl_cffi + __NEXT_DATA__ extraction
├── parser_autoscout24.py # AS24: HTTP primary + Playwright fallback
├── parser_sweden.py     # Bytbil (httpx) + Blocket (Playwright)
├── parser_mobilede.py   # Mobile.de REST API
├── browser_pool.py      # Singleton Playwright (only for Blocket fallback)
├── resilience.py        # Circuit breaker per source
└── rate_limiter.py      # Token bucket per domain
```

## Key Discovery (April 2026 HAR Analysis)

- **AutoScout24 = Next.js** with `__NEXT_DATA__` in HTML (20 listings/page with full data)
- **GraphQL auth:** `Basic as24-search-funnel:vnrfbbBjI32Ol1Wka6uNHRp3EYn4dj` (frontend hardcoded)
- **Mobile.de refdata API is OPEN:** `services.mobile.de/refdata/` — 285 makes, all models, no auth
- **Blocket = Podium microservices** (not Next.js), `blocket-api` package works

## Endpoints

| Method | Path | Purpose | Speed |
|--------|------|---------|-------|
| GET | `/search/instant` | Smart search (primary) | <5ms cache / 2-5s cold |
| GET | `/search/stream` | SSE streaming | 0.5s first results |
| POST | `/search` | Legacy async (backward compat) | 2-5s |
| POST | `/hot` | Hot deals refresh | ~30s |
| GET | `/health` | Health + cache stats | instant |
| GET | `/stats` | Cache + circuit breakers | instant |
| GET | `/job/{id}` | Job status | instant |

## Rules
- All normalize functions in `base.py` — don't duplicate
- Deduplication only in `orchestrator.py:deduplicate()` — single source of truth
- All searches go through `orchestrator.search()` — never call parsers directly
- Swedish features go through `translate_and_categorize_features()`
- Use `loguru` (never print), `httpx` for HTTP (never requests), `curl_cffi` for Akamai bypass
