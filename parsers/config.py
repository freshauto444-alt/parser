# parsers/config.py
# Single source of truth for ALL parser configuration.

# ── Price / Year / Mileage limits ────────────────────────────────────────────
MIN_YEAR = 2018
MIN_PRICE_EUR = 20000
MAX_MILEAGE = 200000

# ── Currency ─────────────────────────────────────────────────────────────────
SEK_TO_EUR = 0.088  # Default fallback

async def get_sek_to_eur() -> float:
    """Fetch live SEK→EUR rate, fallback to hardcoded."""
    import httpx as _httpx
    import time as _time
    # Simple module-level cache
    if not hasattr(get_sek_to_eur, "_cache"):
        get_sek_to_eur._cache = (0.0, 0)  # (rate, timestamp)
    rate, ts = get_sek_to_eur._cache
    if rate and (_time.time() - ts) < 86400:  # 24h cache
        return rate
    try:
        async with _httpx.AsyncClient(timeout=5) as client:
            r = await client.get("https://api.exchangerate-api.com/v4/latest/SEK")
            new_rate = r.json()["rates"]["EUR"]
            get_sek_to_eur._cache = (new_rate, _time.time())
            return new_rate
    except Exception:
        return SEK_TO_EUR  # fallback

# ── Per-source rate limits (requests per minute) ─────────────────────────────
# AS24 was 5/min — too conservative, made 8-car detail enrichment hit cache timeout.
# With chrome136 TLS fingerprint + 2-3s jitter retry, 30/min is safe. Observable
# in /metrics — bump down if we see "blocked" kind spike.
RATE_LIMITS = {
    "autoscout24.com": 30,
    "bytbil.com": 40,
    "blocket.se": 30,
    "mobile.de": 30,
}

# ── Circuit breaker ──────────────────────────────────────────────────────────
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_RECOVERY_SECONDS = 300

# ── Cache TTLs ───────────────────────────────────────────────────────────────
CACHE_TTL_SECONDS = 7200         # 2 hours — memory cache
CACHE_STALE_SECONDS = 3600       # 1 hour — after this, background refresh
CACHE_MAX_ENTRIES = 500

# ── Hot deals cron ───────────────────────────────────────────────────────────
HOT_SEARCHES = [
    {"brand": "bmw", "model": "3er", "year_from": 2018},
    {"brand": "audi", "model": "a4", "year_from": 2018},
    {"brand": "mercedes-benz", "model": "c-class", "year_from": 2018},
    {"brand": "volkswagen", "model": "golf", "year_from": 2018},
    {"brand": "volvo", "model": "v60", "year_from": 2018},
    {"brand": "toyota", "model": "rav4", "year_from": 2018},
    {"brand": "porsche", "model": "cayenne", "year_from": 2018},
]
HOT_SEARCHES_PER_MODEL = 8
MAX_HOT_DEALS = 60
MAX_PER_SOURCE = 30

# ── Expiration ───────────────────────────────────────────────────────────────
HOT_DEALS_EXPIRES_HOURS = 24
CUSTOM_SEARCH_EXPIRES_HOURS = 7 * 24

# ── Scraping defaults ────────────────────────────────────────────────────────
DEFAULT_MAX_RESULTS = 20
DEFAULT_MAX_PAGES = 3
DETAIL_FETCH_CONCURRENCY = 5
