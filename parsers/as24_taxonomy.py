"""
AS24 taxonomy lookup — resolves (brand, model) → cat={ma+gr+mt} code by
querying the as24_brands / as24_groups / as24_motors tables harvested by
parsers.harvest_as24_taxonomy.

Loaded lazily once per process and cached in module-level dicts. Total
footprint ≈ 1 MB. Rebuild requires a parser restart; data is stable
enough that weekly cron re-harvest + restart is plenty.

Resolution strategy:
  1. Exact group match by label_norm     (handles "m3", "rs6", "ceed")
  2. AMG-style base+motor for Mercedes   (handles "e 63" → e-klasse mt467)
  3. None  → caller falls back to the legacy hardcoded slug map
"""
from __future__ import annotations

import os
import re
import threading
from typing import Optional

from loguru import logger

# ── Module cache ──────────────────────────────────────────────────────────────
_LOCK = threading.Lock()
_LOADED = False
_BRANDS: dict[str, int] = {}                          # slug → make_id
_GROUPS: dict[tuple[str, str], int] = {}              # (slug, label_norm) → group_id
_MOTORS: dict[tuple[str, int], list[dict]] = {}       # (slug, group_id) → motor records


def _read_all(supabase, table: str, page_size: int = 1000) -> list[dict]:
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


def _normalize_model(model: str) -> str:
    """Lowercase + collapse whitespace, mirrors harvester's normalize_label."""
    return re.sub(r"\s+", " ", model.lower()).strip()


def _ensure_loaded() -> bool:
    """Lazy-load taxonomy from Supabase on first call. Returns True on success."""
    global _LOADED
    if _LOADED:
        return True
    with _LOCK:
        if _LOADED:
            return True
        url = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY")
        if not url or not key:
            logger.warning("[as24_taxonomy] SUPABASE creds missing — skipping cat lookup")
            return False
        try:
            from supabase import create_client
            sb = create_client(url, key)

            brands = _read_all(sb, "as24_brands")
            for b in brands:
                _BRANDS[b["slug"]] = int(b["make_id"])

            groups = _read_all(sb, "as24_groups")
            for g in groups:
                _GROUPS[(g["brand_slug"], g["label_norm"])] = int(g["group_id"])

            motors = _read_all(sb, "as24_motors")
            for m in motors:
                key_t = (m["brand_slug"], int(m["group_id"]))
                _MOTORS.setdefault(key_t, []).append(m)

            _LOADED = True
            logger.info(
                f"[as24_taxonomy] loaded {len(_BRANDS)} brands / "
                f"{len(_GROUPS)} groups / {sum(len(v) for v in _MOTORS.values())} motors"
            )
            return True
        except Exception as e:
            logger.warning(f"[as24_taxonomy] load failed: {e} — falling back to hardcoded slugs")
            return False


def _slugify_brand(brand: str) -> str:
    """Same slug rule used everywhere in the parser: lower-cased, ws→hyphen."""
    return re.sub(r"\s+", "-", brand.lower()).strip("-")


# Mercedes class shorthand: "c-klasse" or "c-class" → bare letter prefix used
# in performance trim recognition.
_MB_CLASS_RX = re.compile(r"^([a-z]{1,3})[\s-]?(?:klasse|class)$", re.IGNORECASE)
# Performance-trim pattern: "e 63", "c 43", "s 65", "a 45", "gle 63" — letter(s)
# + 1-3 digits.  Used to derive the base class + numeric trim for motor lookup.
_PERF_RX = re.compile(r"^([a-z]{1,4})\s*(\d{2,3})(?:\s+(?:amg|s))?$", re.IGNORECASE)


def _resolve_motor(brand_slug: str, group_id: int, trim_digits: str) -> Optional[int]:
    """Look up motortype_id whose label_norm contains the requested digits."""
    motors = _MOTORS.get((brand_slug, group_id), [])
    candidates = [
        m for m in motors
        if m.get("model_label_norm") and trim_digits in m["model_label_norm"]
    ]
    if not candidates:
        return None
    # Pick the entry with most listings — most representative.
    candidates.sort(key=lambda m: -(m.get("listings_count") or 0))
    return int(candidates[0]["motortype_id"])


def resolve_cat(brand: str, model: str) -> Optional[str]:
    """
    Build a cat-code (e.g. 'ma47gr100061mt467') for the given brand/model.
    Returns None when no taxonomy match — caller should fall back to the
    hardcoded AS24_MODEL_SLUG / AS24_MODEL_CAT maps.
    """
    if not brand or not model:
        return None
    if not _ensure_loaded():
        return None

    brand_slug = _slugify_brand(brand)
    make_id = _BRANDS.get(brand_slug)
    if not make_id:
        return None

    model_norm = _normalize_model(model)

    # 1. Exact group match — handles "ceed", "m3", "rs6", "golf", "passat".
    group_id = _GROUPS.get((brand_slug, model_norm))
    if group_id is not None:
        return f"ma{make_id}gr{group_id}"

    # 2. Mercedes-style AMG trim: "e 63" → base class "e-klasse" + motor 467.
    perf = _PERF_RX.match(model_norm)
    if perf:
        letter, digits = perf.group(1).lower(), perf.group(2)
        # Try "{letter}-klasse" first (Mercedes), then bare letter group.
        for base_label in (f"{letter}-klasse", letter):
            base_id = _GROUPS.get((brand_slug, base_label))
            if base_id is None:
                continue
            mt = _resolve_motor(brand_slug, base_id, digits)
            if mt is not None:
                return f"ma{make_id}gr{base_id}mt{mt}"
            return f"ma{make_id}gr{base_id}"

    # 3. Mercedes class slug fallback for inputs that already use full label:
    #    "c-klasse", "e-klasse" without numbers — normally caught by step 1
    #    but handle case where harvester stored variants ("c-klasse" vs "c klasse").
    mb_class = _MB_CLASS_RX.match(model_norm)
    if mb_class:
        cls = mb_class.group(1).lower()
        for label in (f"{cls}-klasse", f"{cls} klasse"):
            gid = _GROUPS.get((brand_slug, label))
            if gid is not None:
                return f"ma{make_id}gr{gid}"

    return None


def get_make_id(brand: str) -> Optional[int]:
    """Public helper: look up only the make_id (without group/motor)."""
    if not _ensure_loaded():
        return None
    return _BRANDS.get(_slugify_brand(brand))
