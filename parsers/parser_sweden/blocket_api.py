# parsers/parser_sweden/blocket_api.py
# Blocket.se "mobility" search API → list[ParsedCar]. Extracted from parser_sweden.py.

import asyncio
import re
from typing import Optional

import httpx
from loguru import logger

from ..base import (
    ParsedCar,
    normalize_make, normalize_model, normalize_color, normalize_body_type,
    normalize_drive, normalize_fuel, normalize_transmission,
    parse_fuel_from_text, extract_uuid, decode_html, clean_int,
    calc_score, sek_to_eur, is_known_brand,
    translate_and_categorize_features, FUEL_UA,
)
from .blocket_variants import resolve_variant

# Performance trims that Blocket's `variant` tree does NOT model as their own
# node (it stops at engine level), so we can't target them precisely. When the
# requested model names one of these, we keep free-text `q` and post-filter the
# returned cars on the token. Word-boundary regex avoids matching e.g. the "r"
# in "Touareg". Mapping: normalized-model token → spec-text regex.
_BLOCKET_TRIM_TOKENS = {
    "gti": r"\bgti\b",
    "gtd": r"\bgtd\b",
    "vz": r"\bvz\b",
    "n": r"\bn\b",          # Hyundai i30 N / i20 N
    "r": r"\br\b",          # VW Golf R (also matches "R-Line" — acceptable)
}

# ══════════════════════════════════════════════════════════════════════════════
#  BLOCKET.SE — "mobility" search API (the real structured endpoint)
# ══════════════════════════════════════════════════════════════════════════════
# Behind /mobility/search/car sits a JSON API that returns rich, structured
# docs[] per car. The OLD path (Playwright list + httpx ld+json detail) targeted
# Blocket's pre-migration Next.js site, so it returned 0 for body/color/hp/drive/
# etc. and ignored fuel/body/transmission filters → wrong-class results. This API
# fixes both: every client filter maps to a real query param (server-side), and
# each result already carries make/model/series/spec/year/mileage/price/fuel/
# transmission/VIN/image. Codes below are from the live `filters` facets.
BLOCKET_SEARCH_URL = "https://www.blocket.se/mobility/search/api/search/SEARCH_ID_CAR_USED"

# our canonical English body → Blocket body_type code
_BLOCKET_BODY = {
    "estate": "4", "sedan": "3", "suv": "9", "hatchback": "2",
    "coupe": "6", "convertible": "7", "van": "5", "pickup": "8",
}
# our canonical fuel → Blocket fuel code (Hybrid → "Hybrid bensin"=6)
_BLOCKET_FUEL = {"petrol": "1", "diesel": "2", "electric": "4", "hybrid": "6"}
_BLOCKET_TRANS = {"automatic": "2", "manual": "1"}
_BLOCKET_DRIVE = {"awd": "2", "fwd": "3", "rwd": "1"}  # Fyrhjuls/Fram/Bakhjuls

# Minimal headers for the JSON API. Crucially NO brotli in Accept-Encoding:
# httpx can't transparently decode `br` unless the brotli package is installed,
# and the shared _HEADERS requests it → the API replies brotli → resp.json()
# chokes on raw bytes ("utf-8 codec can't decode 0x90"). gzip/deflate only is
# safe everywhere. (The bundled blocket_api works for the same reason — UA only.)
_BLOCKET_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
}

# Blocket doc `fuel` text → our canonical
_SE_FUEL_TEXT = {
    "bensin": "Petrol", "diesel": "Diesel", "el": "Electric",
    "hybrid bensin": "Hybrid", "hybrid diesel": "Hybrid",
    "plug-in bensin": "Hybrid", "plug-in diesel": "Hybrid",
    "fordonsgas (cng)": "Petrol", "etanol (ffv, e85)": "Petrol",
}


def _blocket_make_code(brand: str) -> Optional[str]:
    """Brand name → Blocket `variant` make code (e.g. 'Audi' → '0.744').

    Codes come from blocket_api's bundled CarModel enum (~144 makes). Match by
    alphanumeric-normalized name so 'Mercedes-Benz' ↔ MERCEDES_BENZ, etc.
    """
    if not brand:
        return None
    try:
        from blocket_api.constants import CarModel
    except Exception:
        return None
    norm = re.sub(r"[^a-z0-9]", "", brand.lower())
    if not norm:
        return None
    for m in CarModel:
        if re.sub(r"[^a-z0-9]", "", m.name.lower()) == norm:
            return m.value
    return None


def _blocket_spec_hp_cc(spec: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Pull horsepower + engine displacement from `model_specification`.

    e.g. "3.0 TDI V6 Q 272hk" → hp=272, cc=3000, engine="3.0L".
    Conservative on displacement: only a real decimal litre figure ("2.0",
    "3.0") counts — Audi's "45 TDI"/"55 TFSI" trim numbers have no decimal and
    are correctly ignored.
    """
    hp = cc = None
    engine = None
    if not spec:
        return hp, cc, engine
    m = re.search(r"(\d{2,4})\s*hk", spec, re.IGNORECASE)
    if m:
        v = int(m.group(1))
        if 30 <= v <= 1500:
            hp = v
    m2 = re.search(r"\b(\d\.\d)\b", spec)
    if m2:
        liters = float(m2.group(1))
        if 0.6 <= liters <= 8.0:
            cc = int(round(liters * 1000))
            engine = f"{liters}L"
    return hp, cc, engine


def _blocket_doc_to_car(
    it: dict, rate: float,
    body_en: Optional[str], body_ua: Optional[str], drive: Optional[str],
) -> Optional[ParsedCar]:
    try:
        make = normalize_make(it.get("make") or "")
        if not make:
            return None
        model_name = normalize_model(it.get("model") or it.get("heading") or "", make)

        price_sek = (it.get("price") or {}).get("amount")
        price_eur = round(price_sek * rate) if price_sek else None

        mileage = it.get("mileage")
        if mileage is not None:
            mileage = int(mileage)
            if str(it.get("mileage_unit", "")).upper() == "SCANDINAVIAN_MILE":
                mileage *= 10  # Swedish "mil" → km

        fuel_txt = (it.get("fuel") or "").strip()
        fuel_en = _SE_FUEL_TEXT.get(fuel_txt.lower()) or normalize_fuel(fuel_txt)

        spec = it.get("model_specification") or ""
        hp, cc, engine = _blocket_spec_hp_cc(spec)

        img = (it.get("image") or {}).get("url")
        url = it.get("canonical_url") or f"https://www.blocket.se/mobility/item/{it.get('id', '')}"
        seg = it.get("dealer_segment")
        seller = "dealer" if seg == "Företag" else "private" if seg == "Privat" else None

        car = ParsedCar(
            source_site="blocket.se",
            source_url=url,
            external_id=str(it.get("id") or it.get("ad_id") or ""),
            make=make, model=model_name,
            year=it.get("year"),
            price_eur=price_eur, price_original=price_sek, price_currency="SEK",
            mileage_km=mileage,
            fuel=fuel_en, fuel_ua=FUEL_UA.get(fuel_en or "", None),
            transmission=normalize_transmission(it.get("transmission") or ""),
            horsepower=hp, engine=engine, engine_cc=cc,
            drive=normalize_drive(drive) if drive else None,
            body_type=body_en, body_type_ua=body_ua,
            image=img,
            location=it.get("location"),
            country="Sweden",
            vin=(it.get("chassis_number") or None),
            title_line=spec or it.get("heading"),
            dealer_name=it.get("organisation_name"),
            seller_type=seller,
            vehicle_type="Car",
        )
        car.score = calc_score(
            year=car.year, mileage=car.mileage_km, price_eur=car.price_eur,
            has_image=bool(car.image), hp=car.horsepower,
            has_color=bool(car.color), has_drive=bool(car.drive),
            has_body_type=bool(car.body_type), seller_type=car.seller_type,
        )
        return car
    except Exception as e:
        logger.debug(f"[blocket:api] doc parse: {e}")
        return None


_SE_DRIVE_TEXT = {"fyrhjulsdrift": "AWD", "framhjulsdrift": "FWD", "bakhjulsdrift": "RWD"}


def _parse_motorvolym(s: str) -> Optional[int]:
    """Blocket 'Motorvolym' → engine_cc. '2 L'/'2.0 L'→2000, '1968 cm³'→1968."""
    t = str(s).lower()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*l\b", t)
    if m:
        cc = int(round(float(m.group(1).replace(",", ".")) * 1000))
        return cc if 600 <= cc <= 8500 else None
    m = re.search(r"(\d{3,5})\s*cm", t)
    if m:
        cc = int(m.group(1))
        return cc if 600 <= cc <= 8500 else None
    return None


def _apply_blocket_spec(car: ParsedCar, spec: dict, equipment: list) -> None:
    """Merge a CarAd `specifications` dict + equipment into a ParsedCar (detail
    enrichment). Real per-car values OVERRIDE the query-tagged body/drive."""
    if bt := spec.get("Biltyp"):
        en, ua = normalize_body_type(bt)
        if en:
            car.body_type, car.body_type_ua = en, ua
    if col := (spec.get("Färg") or spec.get("Färgbeskrivning")):
        en, ua = normalize_color(col)
        if en:
            car.color, car.color_ua = en, ua
    if (s := spec.get("Säten")) and str(s).strip().isdigit():
        car.seats = int(str(s).strip())
    if (dr := spec.get("Antal dörrar")) and str(dr).strip().isdigit():
        car.doors = int(str(dr).strip())
    if dh := spec.get("Drivhjul"):
        car.drive = _SE_DRIVE_TEXT.get(str(dh).strip().lower()) or car.drive
    if eff := spec.get("Effekt"):  # "190 Hk"
        m = re.search(r"(\d+)", str(eff))
        if m and 30 <= int(m.group(1)) <= 1500:
            car.horsepower = int(m.group(1))
    if mv := spec.get("Motorvolym"):  # "2 L" → cc + text label
        cc = _parse_motorvolym(mv)
        if cc:
            if not car.engine_cc:
                car.engine_cc = cc
            if not car.engine:  # Blocket has no engine text field; derive from cc
                car.engine = f"{cc / 1000:.1f}L"
    if (vin := spec.get("Chassinummer")) and not car.vin:
        car.vin = str(vin).upper()
    if equipment:
        feats = translate_and_categorize_features(equipment)
        car.safety_features = feats["safety"]
        car.comfort_features = feats["comfort"]
        car.infotainment = feats["infotainment"]
        car.features_other = feats["other"]


def _blocket_description(html: str) -> Optional[str]:
    """Seller free-text from the detail page. The bundled CarAd selector is stale
    (returns nothing); the text lives in a <section> under an <h2> 'Beskrivning'."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for h in soup.find_all(["h2", "h3"]):
            if "beskrivning" in h.get_text(strip=True).lower():
                sec = h.find_parent("section") or h.parent
                if not sec:
                    return None
                txt = re.sub(r"(?i)^beskrivning\s*", "", sec.get_text("\n", strip=True)).strip()
                return txt[:4000] if len(txt) >= 15 else None  # skip "-"/placeholder junk
    except Exception:
        pass
    return None


async def _blocket_enrich(cars: list[ParsedCar], client: httpx.AsyncClient, budget_s: float = 6.0) -> None:
    """Best-effort detail enrichment: fetch /mobility/item/{id} per car and merge
    body/color/seats/drive/hp/engine_cc/VIN/features via the bundled CarAd parser.
    Bounded (sem) + time-boxed — cars not enriched in time keep their search
    fields. Never raises, never drops cars."""
    from blocket_api.ad_parser import CarAd
    targets = [c for c in cars if (c.external_id or "").isdigit()]
    if not targets:
        return
    sem = asyncio.Semaphore(8)

    async def one(car: ParsedCar) -> None:
        async with sem:
            try:
                resp = await client.get(
                    f"https://www.blocket.se/mobility/item/{car.external_id}",
                    headers={"Accept": "text/html"}, timeout=8,
                )
                if resp.status_code != 200:
                    return
                d = CarAd(int(car.external_id)).parse(resp)  # sync BeautifulSoup parse
                _apply_blocket_spec(car, d.get("specifications", {}) or {}, d.get("equipment", []) or [])
                # Gallery: the search doc gives ONE photo; the detail page carries
                # all of them. Pull this item's image UUIDs (1280w = smallest valid
                # CDN preset; 1024w 404s) → image + gallery.
                uuids = list(dict.fromkeys(re.findall(
                    rf'images\.blocketcdn\.se/dynamic/[^/"]+/item/{car.external_id}/([0-9a-f-]{{36}})',
                    resp.text,
                )))[:20]
                if uuids:
                    urls = [f"https://images.blocketcdn.se/dynamic/1280w/item/{car.external_id}/{u}" for u in uuids]
                    car.image = urls[0]
                    car.gallery = urls[1:]
                if desc := _blocket_description(resp.text):
                    car.description = desc
            except Exception as e:
                logger.debug(f"[blocket:detail] {car.external_id}: {e}")

    tasks = [asyncio.create_task(one(c)) for c in targets]
    done, pending = await asyncio.wait(tasks, timeout=budget_s)
    for t in pending:
        t.cancel()
    logger.info(f"[blocket:api] enriched {len(done)}/{len(targets)} via detail (budget {budget_s}s)")


async def parse_blocket_api(
    rate: float, *,
    brand: str = "", model: str = "",
    year_from: int = 2018, year_to: Optional[int] = None,
    price_from_eur: Optional[int] = None, price_to_eur: Optional[int] = None,
    max_results: int = 30,
    fuel: Optional[str] = None, transmission: Optional[str] = None,
    body_type: Optional[str] = None, drive: Optional[str] = None,
    vehicle_type: str = "Car",
) -> list[ParsedCar]:
    """Blocket via its structured search API. Pure HTTP (no Playwright).

    Brand → exact `variant` make code; model → deep `variant` model/trim code
    when the taxonomy (blocket_variants.json) has one (e.g. "X5 M"→2.749.2466.8360),
    else free-text `q` + an optional perf-trim post-filter (gti/gtd/r/n/vz absent
    from the tree). body/fuel/transmission/wheel_drive/price/year all map to
    server-side params; body & drive are tagged from the query.
    """
    base_params: list[tuple[str, object]] = []
    # trim_token: set when we fall back to free-text for a performance trim that
    # Blocket can't target via `variant` (gti/gtd/r/n/vz) → post-filter on it.
    trim_token: Optional[str] = None
    make_code = _blocket_make_code(brand)
    if make_code:
        # Prefer the DEEP model/trim `variant` code (e.g. "X5 M"→2.749.2466.8360)
        # over make-only `variant` + free-text `q`: free-text returns base-model
        # pollution ("X5 M" → base X5), the deep code does not.
        deep_code = resolve_variant(make_code, model) if model else None
        if deep_code:
            base_params.append(("variant", deep_code))
        else:
            # No deep trim node (Golf GTI/R, i30 N… are absent from Blocket's tree).
            # Narrow as far as we can: resolve the BASE model (model minus the trim
            # word) to its series/model node so Blocket searches within e.g. Golf
            # instead of the whole make — make-level free-text returns a "солянка"
            # of every VW. Then keep the free-text trim word + a post-filter on it.
            model_norm = re.sub(r"[^a-z0-9]+", " ", model.lower()).strip() if model else ""
            trim_in = next((tok for tok in _BLOCKET_TRIM_TOKENS
                            if tok in model_norm.split()), None)
            base_node = None
            if trim_in and model:
                base_model = " ".join(t for t in model.split() if t.lower() != trim_in)
                base_node = resolve_variant(make_code, base_model) if base_model else None
            base_params.append(("variant", base_node or make_code))
            if model:
                base_params.append(("q", model))
                trim_token = trim_in
    else:
        q = f"{brand} {model}".strip()
        if q:
            base_params.append(("q", q))

    if year_from:
        base_params.append(("year_from", year_from))
    if year_to:
        base_params.append(("year_to", year_to))
    if price_from_eur and rate > 0:
        base_params.append(("price_from", int(round(price_from_eur / rate))))
    if price_to_eur and rate > 0:
        base_params.append(("price_to", int(round(price_to_eur / rate))))

    if body_type and (bc := _BLOCKET_BODY.get(body_type.strip().lower())):
        base_params.append(("body_type", bc))
    if fuel and (fc := _BLOCKET_FUEL.get(fuel.strip().lower())):
        base_params.append(("fuel", fc))
    if transmission and (tc := _BLOCKET_TRANS.get(transmission.strip().lower())):
        base_params.append(("transmission", tc))
    if drive and (dc := _BLOCKET_DRIVE.get(drive.strip().lower())):
        base_params.append(("wheel_drive", dc))
    # Only USED cars for sale (sales_form 1) — drops leasing (5) and new-car (2)
    # listings whose monthly/config prices leaked in as junk (e.g. "2027, €704").
    base_params.append(("sales_form", "1"))

    # We filtered by body server-side → safe to tag every result with it.
    body_en, body_ua = normalize_body_type(body_type) if body_type else (None, None)

    # Compiled trim post-filter (only when free-text fallback hit a perf trim).
    trim_re = re.compile(_BLOCKET_TRIM_TOKENS[trim_token], re.IGNORECASE) if trim_token else None

    cars: list[ParsedCar] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(headers=_BLOCKET_API_HEADERS, follow_redirects=True) as client:
        page = 1
        # Loop already scales with max_results (now 120 via PER_SOURCE) up to the
        # 10-page cap and honours the API's own `last` page — so raising PER_SOURCE
        # alone deepens Blocket pagination. The page<=10 cap stays: at ~30-40
        # docs/page it can already surface 300+ docs, far past what we need.
        while len(cars) < max_results and page <= 10:
            try:
                resp = await client.get(
                    BLOCKET_SEARCH_URL, params=base_params + [("page", page)], timeout=15,
                )
                if resp.status_code != 200:
                    logger.warning(f"[blocket:api] HTTP {resp.status_code} page {page}")
                    break
                data = resp.json()
            except Exception as e:
                logger.warning(f"[blocket:api] page {page}: {e}")
                break

            docs = data.get("docs") or []
            if not docs:
                break
            for it in docs:
                ext = str(it.get("id") or it.get("ad_id") or "")
                if ext and ext in seen:
                    continue
                car = _blocket_doc_to_car(it, rate, body_en, body_ua, drive)
                if car:
                    # Drop base-model cars when we asked for a perf trim Blocket
                    # can't target: the trim token must appear in the model+spec
                    # text (e.g. "gti" for VW Golf GTI).
                    if trim_re:
                        hay = f"{it.get('model') or ''} {it.get('model_specification') or ''} {it.get('heading') or ''}"
                        if not trim_re.search(hay):
                            continue
                    seen.add(ext)
                    cars.append(car)
                if len(cars) >= max_results:
                    break

            paging = (data.get("metadata") or {}).get("paging") or {}
            last = paging.get("last") or page
            if page >= last:
                break
            page += 1

        # Detail enrichment (best-effort, time-boxed, reuses the open client):
        # fills body/color/seats/drive/hp/engine_cc/VIN/features the search docs
        # lack. Capped to the cars we'll surface to bound bandwidth/latency.
        if cars:
            await _blocket_enrich(cars[:max_results], client)

    logger.info(f"[blocket:api] {brand} {model}: {len(cars)} cars")
    return cars[:max_results]


