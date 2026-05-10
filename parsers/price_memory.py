"""
parsers/price_memory.py
Price Memory — learns and remembers market prices from every search.

Every time a user searches and parser returns results, this module:
1. Records price stats per make+model+year+fuel (median, min, max, count)
2. Tracks price trends over time (weekly averages)
3. Provides instant price lookup for AI suggestions

Data stored in Supabase table `price_memory` — survives restarts.
Gets smarter with every user search.
"""

import os
from datetime import datetime, timezone
from typing import Optional
from loguru import logger


def _get_supabase():
    from supabase import create_client
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


# Process-wide flag: once we've detected the table is missing we back off *temporarily*.
# Previous version set this forever (one transient error disabled price-learning for the
# rest of the process). Now we re-check every PRICE_MEMORY_RETRY_SECS.
import time as _time
_TABLE_MISSING_UNTIL: float = 0.0
PRICE_MEMORY_RETRY_SECS = 600  # 10 min — enough to avoid log spam, short enough to recover


def _table_missing_error(err: Exception) -> bool:
    msg = str(err)
    return "price_memory" in msg and (
        "PGRST205" in msg
        or "does not exist" in msg
        or "42P01" in msg
        or "schema cache" in msg
    )


def record_prices(cars: list, source: str = "search"):
    """
    Record prices from search results into price_memory.
    Called after every successful search.

    Groups by make+model+fuel, computes stats, upserts into DB.
    """
    global _TABLE_MISSING_UNTIL  # declare up-front (read + possibly write)
    if not cars:
        return
    if _TABLE_MISSING_UNTIL > _time.monotonic():
        return

    # Group by make+model+fuel
    groups: dict[str, list[dict]] = {}
    for car in cars:
        # Handle both ParsedCar objects and dicts
        if hasattr(car, "make"):
            make = car.make or ""
            model = car.model or ""
            price = car.price_eur
            year = car.year
            fuel = car.fuel or "Unknown"
            mileage = car.mileage_km
        else:
            make = car.get("make", "")
            model = car.get("model", "")
            price = car.get("price") or car.get("price_eur")
            year = car.get("year")
            fuel = car.get("fuel", "Unknown")
            mileage = car.get("mileage") or car.get("mileage_km")

        if not make or not price or price < 1000 or price > 500000:
            continue

        key = f"{make}|{model}|{fuel}".lower()
        if key not in groups:
            groups[key] = []
        groups[key].append({
            "price": float(price),
            "year": year,
            "mileage": int(mileage) if mileage else None,
        })

    if not groups:
        return

    try:
        supabase = _get_supabase()
        now = datetime.now(timezone.utc).isoformat()

        for key, items in groups.items():
            parts = key.split("|")
            make, model, fuel = parts[0], parts[1], parts[2]

            prices = sorted([i["price"] for i in items])
            years = [i["year"] for i in items if i["year"]]
            mileages = [i["mileage"] for i in items if i["mileage"]]

            # Trim outliers (10% each side)
            trim = max(1, len(prices) // 10)
            if len(prices) > 4:
                trimmed = prices[trim:-trim]
            else:
                trimmed = prices

            if not prices:
                continue  # skip groups with no valid prices
            median_price = trimmed[len(trimmed) // 2] if trimmed else prices[0]
            avg_mileage = int(sum(mileages) / len(mileages)) if mileages else None

            row = {
                "make": make.title(),
                "model": model,
                "fuel": fuel.title() if fuel != "Unknown" else None,
                "price_min": int(trimmed[0]) if trimmed else int(prices[0]),
                "price_max": int(trimmed[-1]) if trimmed else int(prices[-1]),
                "price_median": int(median_price),
                "price_avg": int(sum(trimmed) / len(trimmed)) if trimmed else int(prices[0]),
                "sample_count": len(prices),
                "year_min": min(years) if years else None,
                "year_max": max(years) if years else None,
                "avg_mileage_km": avg_mileage,
                "last_updated": now,
                "source": source,
            }

            # Upsert by make+model+fuel
            try:
                supabase.table("price_memory").upsert(
                    row,
                    on_conflict="make,model,fuel",
                ).execute()
            except Exception as e:
                if _table_missing_error(e):
                    # Try auto-create once; if that fails too, disable for this process.
                    try:
                        _create_table(supabase)
                        supabase.table("price_memory").upsert(row, on_conflict="make,model,fuel").execute()
                    except Exception as e2:
                        _TABLE_MISSING_UNTIL = _time.monotonic() + PRICE_MEMORY_RETRY_SECS
                        logger.warning(
                            "[price_memory] Table missing and auto-create failed — "
                            f"backing off for {PRICE_MEMORY_RETRY_SECS}s. Create it manually in Supabase. "
                            f"Last error: {e2}"
                        )
                        return
                else:
                    logger.debug(f"[price_memory] Upsert failed: {e}")

        logger.debug(f"[price_memory] Recorded {len(groups)} model price stats from {len(cars)} cars")

    except Exception as e:
        logger.warning(f"[price_memory] Failed to record: {e}")


def get_price_guide() -> dict[str, dict]:
    """
    Get all remembered prices as a dict.
    Returns: { "bmw 320": { min: 25000, max: 38000, median: 31000, count: 45 }, ... }
    """
    try:
        supabase = _get_supabase()
        result = supabase.table("price_memory").select("*").execute()
        guide = {}
        for row in result.data or []:
            key = f"{row['make']} {row['model']}".lower()
            guide[key] = {
                "min": row["price_min"],
                "max": row["price_max"],
                "median": row["price_median"],
                "avg": row["price_avg"],
                "count": row["sample_count"],
                "fuel": row.get("fuel"),
                "year_min": row.get("year_min"),
                "year_max": row.get("year_max"),
                "avg_mileage": row.get("avg_mileage_km"),
            }
        return guide
    except Exception as e:
        logger.warning(f"[price_memory] Failed to load: {e}")
        return {}


def format_price_guide_for_prompt(guide: dict, limit: int = 50) -> str:
    """Format price guide as text for Claude prompt."""
    if not guide:
        return ""
    lines = []
    # Sort by sample count (most data = most reliable)
    sorted_items = sorted(guide.items(), key=lambda x: -x[1]["count"])[:limit]
    for name, data in sorted_items:
        fuel_str = f" ({data['fuel']})" if data.get("fuel") else ""
        year_str = f", {data['year_min']}-{data['year_max']}" if data.get("year_min") else ""
        lines.append(
            f"  {name}{fuel_str}: {data['min']}-{data['max']} EUR "
            f"(median {data['median']}, {data['count']} авто{year_str})"
        )
    return "\n".join(lines)


def _create_table(supabase):
    """Create price_memory table if it doesn't exist."""
    try:
        supabase.postgrest.schema("public").rpc("exec_sql", {
            "sql": """
                CREATE TABLE IF NOT EXISTS price_memory (
                    id SERIAL PRIMARY KEY,
                    make TEXT NOT NULL,
                    model TEXT NOT NULL,
                    fuel TEXT,
                    price_min INT,
                    price_max INT,
                    price_median INT,
                    price_avg INT,
                    sample_count INT DEFAULT 0,
                    year_min INT,
                    year_max INT,
                    avg_mileage_km INT,
                    last_updated TIMESTAMPTZ DEFAULT now(),
                    source TEXT DEFAULT 'search',
                    UNIQUE(make, model, fuel)
                );
            """
        }).execute()
    except Exception:
        # Can't create via RPC — table needs manual creation
        logger.info("[price_memory] Table needs manual creation in Supabase dashboard")
