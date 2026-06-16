# parsers/base/db.py — build_db_row (ParsedCar -> DB dict). From base.py.

import uuid as uuid_lib
from datetime import datetime, timezone
from typing import Optional

from .constants import BODY_UA, FUEL_UA
from .model import ParsedCar
from .categorize import deduplicate_gallery

# ══════════════════════════════════════════════════════════════════════════════
#  DB ROW BUILDER
# ══════════════════════════════════════════════════════════════════════════════

COUNTRY_UA = {
    "Europe": "Європа",
    "Germany": "Німеччина",
    "Sweden": "Швеція",
    "France": "Франція",
    "Italy": "Італія",
    "Spain": "Іспанія",
    "Netherlands": "Нідерланди",
    "Belgium": "Бельгія",
    "Austria": "Австрія",
    "Switzerland": "Швейцарія",
    "Poland": "Польща",
    "Czech Republic": "Чехія",
    "Denmark": "Данія",
    "Norway": "Норвегія",
    "Finland": "Фінляндія",
    "UK": "Великобританія",
    "Ireland": "Ірландія",
    "Portugal": "Португалія",
    "Luxembourg": "Люксембург",
    "Hungary": "Угорщина",
    "Romania": "Румунія",
    "Croatia": "Хорватія",
    "Slovenia": "Словенія",
    "Slovakia": "Словаччина",
    "Bulgaria": "Болгарія",
    "Lithuania": "Литва",
    "Latvia": "Латвія",
    "Estonia": "Естонія",
}


def build_db_row(
    car: ParsedCar,
    source_type: str,
    expires_at: str,
    client_order_id: Optional[str] = None,
    template_id: Optional[str] = None,
) -> dict:
    return {
        "id": str(uuid_lib.uuid4()),
        "source_type": source_type,
        "source_url": car.source_url,
        "source_site": car.source_site,
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
        "make": car.make,
        "model": car.model,
        "year": car.year,
        "price": car.price_eur,
        "mileage": car.mileage_km,
        "fuel": car.fuel or "Petrol",
        "fuel_ua": car.fuel_ua or FUEL_UA.get(car.fuel or "Petrol", "Бензин"),
        "transmission": car.transmission or "Automatic",
        "horsepower": car.horsepower,
        "engine": car.engine,
        "drive": car.drive or "Unknown",
        "body_type": car.body_type or "Unknown",
        "body_type_ua": car.body_type_ua or BODY_UA.get(car.body_type or "", "Невідомо"),
        "color": car.color or "Unknown",
        "color_ua": car.color_ua or "Невідомо",
        "doors": car.doors,
        "seats": car.seats,
        "seat_material": car.seat_material,
        "seat_material_ua": car.seat_material_ua,
        "image": car.image or "",
        "gallery": deduplicate_gallery(car.gallery or []),
        "features": [],
        "features_ua": car.features_other,
        "safety_features": car.safety_features,
        "comfort_features": car.comfort_features,
        "infotainment": car.infotainment,
        "status": "Available",
        "status_ua": "Доступно",
        "verified": False,
        "condition": car.condition,
        "condition_ua": "Вживаний" if car.condition == "Used" else "Новий",
        "country": car.country,
        "country_ua": COUNTRY_UA.get(car.country, car.country),
        "plate_type": "EU",
        "vin": car.vin,
        "engine_cc": car.engine_cc,
        "history": [],
        "client_order_id": client_order_id,
    }
