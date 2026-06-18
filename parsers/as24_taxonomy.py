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
# Body-style suffix that the AI occasionally tacks onto a model name even
# though body_type is supposed to be a separate filter. Strip these before
# group lookup — taxonomy only stores base models ("a6", not "a6 avant").
_BODY_SUFFIX_RX = re.compile(
    r"\s+(?:"
    r"avant|touring|estate|sportback|sw|combi|kombi|"
    r"convertible|cabriolet|cabrio|coupé|coupe|"
    r"hatchback|saloon|sedan|wagon|"
    r"[35][\s-]?door[s]?"
    r")\s*$",
    re.IGNORECASE,
)


def _strip_body_suffix(model_norm: str) -> str:
    return _BODY_SUFFIX_RX.sub("", model_norm).strip()


# AS24 stores BMW series under German labels (3er, not "3 Series") and the
# MINI Cooper hatchback under the "Mini" group label. Map English / informal
# inputs the AI and users actually type to those internal labels.
_GROUP_ALIASES: dict[tuple[str, str], str] = {
    # BMW English → German
    ("bmw", "1 series"): "1er", ("bmw", "2 series"): "2er",
    ("bmw", "3 series"): "3er", ("bmw", "4 series"): "4er",
    ("bmw", "5 series"): "5er", ("bmw", "6 series"): "6er",
    ("bmw", "7 series"): "7er", ("bmw", "8 series"): "8er",
    # MINI: "cooper" maps to the base "Mini" group (Cooper hatchback)
    ("mini", "cooper"): "mini",
    ("mini", "cooper s"): "mini",
    ("mini", "john cooper works"): "mini",
    ("mini", "jcw"): "mini",
    # Mercedes English → German (most already match via label_norm but a few inputs miss)
    ("mercedes-benz", "a-class"): "a-klasse",
    ("mercedes-benz", "b-class"): "b-klasse",
    ("mercedes-benz", "c-class"): "c-klasse",
    ("mercedes-benz", "e-class"): "e-klasse",
    ("mercedes-benz", "g-class"): "g-klasse",
    ("mercedes-benz", "s-class"): "s-klasse",
    ("mercedes-benz", "v-class"): "v-klasse",
    # Tesla
    ("tesla", "model3"): "model 3",
    ("tesla", "models"): "model s",
    ("tesla", "modely"): "model y",
    ("tesla", "modelx"): "model x",
}


def _resolve_motor(brand_slug: str, group_id: int, trim_digits: str) -> Optional[int]:
    """Look up motortype_id whose label_norm contains the requested digits.

    Legacy Mercedes-only digit path (kept for the `_PERF_RX` strategy). Prefer
    `_resolve_motor_exact` for full-model matching across all brands.
    """
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


def _motor_key_variants(model_norm: str) -> set[str]:
    """Normalized forms of a requested model to compare against motor labels.

    Motors are stored with `model_label_norm` like "x5 m", "c 63 amg",
    "golf gti". The AI/user may type "c63", "c 63", "c 63 amg" — all the same
    trim. We generate equivalent forms so the *exact* comparison still hits:
      - the raw normalized model ("x5 m")
      - a space-inserted form splitting an alpha-prefix from trailing digits
        so "c63" → "c 63" (AS24 always spaces letter↔digit in motor labels)
    Matching stays exact against this set — we never substring-match, so
    "x5" can't accidentally hit "x5 m" (or vice-versa).
    """
    forms = {model_norm}
    # Insert a space between an alpha run and a following digit run anywhere:
    # "c63" → "c 63", "x5m"(rare) untouched-but-harmless. Collapse afterwards.
    spaced = re.sub(r"([a-z])(\d)", r"\1 \2", model_norm)
    spaced = re.sub(r"(\d)([a-z])", r"\1 \2", spaced)
    forms.add(re.sub(r"\s+", " ", spaced).strip())
    return forms


def _resolve_motor_exact(brand_slug: str, group_id: int, model_norm: str) -> Optional[int]:
    """Look up the motortype_id whose `model_label_norm` EXACTLY equals the
    requested full model (e.g. "x5 m" → mt368, "c 63 amg"/"c63" → mt310).

    Generalized across ALL brands. Conservative: only an exact normalized
    match counts — if nothing matches we return None and the caller keeps the
    group-only cat (never attach a wrong motor). When several rows share the
    label (duplicate generations), the one with the most listings wins.
    """
    motors = _MOTORS.get((brand_slug, group_id), [])
    wanted = _motor_key_variants(model_norm)
    candidates = [
        m for m in motors
        if m.get("model_label_norm")
        and _normalize_model(m["model_label_norm"]) in wanted
    ]
    if not candidates:
        return None
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

    # Build the list of label candidates we'll try against the group table.
    # Order matters — the first hit wins, so we try exact first, then
    # body-stripped, then aliased (so a user typing "BMW 3 series Touring"
    # resolves via strip("touring") → alias("3 series") → "3er").
    candidates: list[str] = []
    def _add(label: str | None):
        if label and label not in candidates:
            candidates.append(label)

    def _add_variants(label: str):
        """Add a label + its alias + space-collapsed form."""
        _add(label)
        _add(_GROUP_ALIASES.get((brand_slug, label)))
        collapsed = label.replace(" ", "").replace("-", "")
        if collapsed != label:
            _add(collapsed)

    _add_variants(model_norm)
    stripped = _strip_body_suffix(model_norm)
    if stripped != model_norm:
        _add_variants(stripped)

    # Robustness against trim / spec / make-prefix noise the AI sometimes tacks on
    # ("SQ5 TDI Competition", "Audi SQ5", "S Q5"). The taxonomy stores only base
    # group labels ("sq5"), so progressively drop trailing tokens and an optional
    # leading make token, trying each shorter prefix. Longest-first → first real
    # group match wins (so "model 3 performance" still resolves to "model 3", not "model").
    tokens = model_norm.split()
    make_first = brand_slug.split("-")[0]  # "audi", "mercedes"
    if len(tokens) > 1 and tokens[0] == make_first:
        _add_variants(" ".join(tokens[1:]))
        tokens = tokens[1:]
    for i in range(len(tokens) - 1, 0, -1):
        _add_variants(" ".join(tokens[:i]))

    for cand in candidates:
        group_id = _GROUPS.get((brand_slug, cand))
        if group_id is not None:
            # The base group resolved (e.g. "x5" → group 16406). Before returning
            # the bare group, check whether the FULL requested model names a
            # specific per-trim motor within that group ("x5 m" → mt368). This
            # is the generalized motor path — works for ALL brands, not just
            # Mercedes. Use the original model_norm (not the shortened candidate)
            # so "x5 m" still resolves its motor even though we matched group via
            # the stripped "x5" candidate. Exact-only → "x5" never picks "x5 m".
            #
            # Only attach a motor when the request is MORE SPECIFIC than the
            # group itself (carries a trim beyond the group label). A bare
            # "x5" == the group label must stay group-wide, even though an
            # equally-named "x5" motor row exists — otherwise we'd wrongly pin
            # the search to one arbitrary engine variant.
            if model_norm != cand:
                mt = _resolve_motor_exact(brand_slug, group_id, model_norm)
                if mt is not None:
                    return f"ma{make_id}gr{group_id}mt{mt}"
            return f"ma{make_id}gr{group_id}"

    # 2. Mercedes-style AMG trim: "e 63" / "c 63 amg" → base class "e-klasse" +
    #    motor. The trim model itself isn't its own group, so it's not caught
    #    above. Resolve the base class group, then attach the motor — first via
    #    the generalized exact full-model match ("c 63 amg" == motor label), then
    #    falling back to the legacy digit-substring match ("e 63" → digits "63").
    perf = _PERF_RX.match(model_norm)
    if perf:
        letter, digits = perf.group(1).lower(), perf.group(2)
        # Try "{letter}-klasse" first (Mercedes), then bare letter group.
        for base_label in (f"{letter}-klasse", letter):
            base_id = _GROUPS.get((brand_slug, base_label))
            if base_id is None:
                continue
            mt = (
                _resolve_motor_exact(brand_slug, base_id, model_norm)
                or _resolve_motor(brand_slug, base_id, digits)
            )
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
