# parsers/base — unified data model + normalization + translation for all parsers.
# Split from the former base.py into focused modules; this package re-exports the
# full public API so `from .base import …` / `from ..base import …` is unchanged.

from .model import ParsedCar
from .constants import (
    KNOWN_BRANDS, MAKE_NORMALIZE, FUEL_NORMALIZE, FUEL_UA,
    COLOR_EN, COLOR_UA, BODY_EN, BODY_UA, DRIVE_NORMALIZE, TRANS_NORMALIZE,
    SV_FEATURES,
)
from .normalize import (
    normalize_make, normalize_model, normalize_color, normalize_body_type,
    normalize_drive, normalize_fuel, normalize_transmission,
    is_known_brand, parse_fuel_from_text, infer_body_from_model,
)
from .features import translate_feature, translate_and_categorize_features
from .helpers import decode_html, clean_int, extract_uuid, sek_to_eur
from .scoring import calc_score, count_premium_features, PREMIUM_FEATURES
from .categorize import categorize_feature, deduplicate_gallery, COUNTRY_CODE_MAP
from .db import build_db_row, COUNTRY_UA

__all__ = [
    "ParsedCar",
    "KNOWN_BRANDS", "MAKE_NORMALIZE", "FUEL_NORMALIZE", "FUEL_UA",
    "COLOR_EN", "COLOR_UA", "BODY_EN", "BODY_UA", "DRIVE_NORMALIZE", "TRANS_NORMALIZE",
    "SV_FEATURES",
    "normalize_make", "normalize_model", "normalize_color", "normalize_body_type",
    "normalize_drive", "normalize_fuel", "normalize_transmission",
    "is_known_brand", "parse_fuel_from_text", "infer_body_from_model",
    "translate_feature", "translate_and_categorize_features",
    "decode_html", "clean_int", "extract_uuid", "sek_to_eur",
    "calc_score", "count_premium_features", "PREMIUM_FEATURES",
    "categorize_feature", "deduplicate_gallery", "COUNTRY_CODE_MAP",
    "build_db_row", "COUNTRY_UA",
]
