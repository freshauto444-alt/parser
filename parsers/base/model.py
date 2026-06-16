# parsers/base/model.py — the ParsedCar dataclass. Extracted from base.py.

from dataclasses import dataclass, field
from typing import Optional

# ══════════════════════════════════════════════════════════════════════════════
#  UNIFIED DATA MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ParsedCar:
    source_site: str
    source_url: str
    external_id: str
    make: str = ""
    model: str = ""
    year: Optional[int] = None
    price_eur: Optional[float] = None
    mileage_km: Optional[int] = None
    fuel: Optional[str] = None
    fuel_ua: Optional[str] = None
    transmission: Optional[str] = None
    horsepower: Optional[int] = None
    drive: Optional[str] = None
    body_type: Optional[str] = None
    body_type_ua: Optional[str] = None
    engine: Optional[str] = None
    color: Optional[str] = None
    color_ua: Optional[str] = None
    doors: Optional[int] = None
    seats: Optional[int] = None
    seat_material: Optional[str] = None
    seat_material_ua: Optional[str] = None
    image: Optional[str] = None
    gallery: list[str] = field(default_factory=list)
    safety_features: list[str] = field(default_factory=list)
    comfort_features: list[str] = field(default_factory=list)
    infotainment: list[str] = field(default_factory=list)
    features_other: list[str] = field(default_factory=list)
    location: Optional[str] = None
    country: str = "Europe"
    condition: str = "Used"
    score: int = 50
    price_original: Optional[float] = None
    price_currency: Optional[str] = None
    # Quality signals — the more of these we have, the better we can rank.
    previous_owners: Optional[int] = None     # 1 owner is premium, 3+ is red flag
    accident_free: Optional[bool] = None      # Unfallfrei / olycksfri / no damage
    service_history: Optional[bool] = None    # Scheckheftgepflegt / full service book
    has_damage: Optional[bool] = None         # explicit damage listed (for filter-out)
    warranty_months: Optional[int] = None     # remaining factory / dealer warranty
    seller_type: Optional[str] = None         # "dealer" / "private" — dealers average better
    inspection_valid_until: Optional[str] = None  # TÜV/besiktning YYYY-MM
    # Multi-vehicle support — "Car" (default), "Motorcycle", "Truck", "Van",
    # "Bus", "Camper", "Trailer", "ConstructionMachine", "Tractor".
    vehicle_type: str = "Car"
    engine_cc: Optional[int] = None  # cubic centimetres — main metric for motorcycles
    vin: Optional[str] = None  # when exposed — unlocks cross-source dedup

    # ── Extended spec / environment ──────────────────────────────────────────
    interior_color: Optional[str] = None          # e.g. "Black leather", "Beige Dakota"
    interior_color_ua: Optional[str] = None
    emission_class: Optional[str] = None          # "Euro 6", "Euro 6d-TEMP"
    co2_emissions_g_km: Optional[int] = None      # g/km CO2 — WLTP
    fuel_consumption_combined: Optional[float] = None  # L/100km or kWh/100km
    energy_efficiency: Optional[str] = None       # "A+", "A", "B" …
    cylinders: Optional[int] = None
    gears: Optional[int] = None                   # 6, 7, 8, 9, 10
    weight_kg: Optional[int] = None               # empty weight
    tow_capacity_kg: Optional[int] = None

    # ── Sales / listing ──────────────────────────────────────────────────────
    description: Optional[str] = None             # seller's free-text description
    title_line: Optional[str] = None              # original listing title
    first_registration: Optional[str] = None      # "MM/YYYY" for display

    # ── Dealer info ──────────────────────────────────────────────────────────
    dealer_name: Optional[str] = None
    dealer_rating: Optional[float] = None         # stars 0..5
    dealer_review_count: Optional[int] = None
    dealer_phone: Optional[str] = None
    dealer_website: Optional[str] = None
    dealer_city: Optional[str] = None
    dealer_zip: Optional[str] = None

    # ── Video (AS24 has it in vehicle.video.src) ────────────────────────────
    video_url: Optional[str] = None

    # Timestamp of listing update (for "fresh" scoring)
    listing_updated_at: Optional[str] = None


