# parsers/db.py
# Збереження ParsedCar в Supabase.
# Єдина точка запису — всі парсери йдуть сюди.

from typing import Optional
from datetime import datetime, timedelta, timezone

from loguru import logger
from supabase import Client

from .base import ParsedCar, build_db_row


def save(
    items: list[ParsedCar],
    supabase: Client,
    source_type: str,
    client_order_id: Optional[str] = None,
    template_id: Optional[str] = None,
    expires_hours: int = 24,
) -> tuple[int, int, int]:
    """
    Зберігає список ParsedCar в Supabase.
    Повертає (saved_new, updated_existing, errors).
    """
    saved = 0
    updated = 0
    errors = 0
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()

    for item in items:
        try:
            # ── Валідація мінімальних полів ────────────────────────────────────
            if not item.source_url:
                logger.warning(f"[db] Пропускаємо запис без source_url: {item.make} {item.model}")
                errors += 1
                continue

            # Fallback: якщо image порожній але є gallery
            if not item.image and item.gallery:
                item.image = item.gallery[0]

            # ── Upsert (atomic — no race condition between select & insert) ────
            row = build_db_row(
                car=item,
                source_type=source_type,
                expires_at=expires_at,
                client_order_id=client_order_id,
                template_id=template_id,
            )
            try:
                # Check if exists first (for metrics)
                existing = supabase.table("cars").select("id").eq("source_url", item.source_url).limit(1).execute()
                is_update = bool(existing.data)

                result = (
                    supabase.table("cars")
                    .upsert(row, on_conflict="source_url")
                    .execute()
                )
                if result.data:
                    if is_update:
                        updated += 1
                    else:
                        saved += 1
                    logger.debug(f"[db] {'Updated' if is_update else 'Saved'}: {item.make} {item.model} ({item.source_site})")
                else:
                    logger.warning(f"[db] Upsert повернув пустий результат для {item.source_url}")
                    errors += 1
            except Exception as upsert_err:
                # Fallback: try update if upsert fails (e.g. no unique constraint)
                logger.debug(f"[db] Upsert failed, trying update: {upsert_err}")
                try:
                    update_data = _build_update(item, expires_at)
                    result = (
                        supabase.table("cars")
                        .update(update_data)
                        .eq("source_url", item.source_url)
                        .execute()
                    )
                    if result.data:
                        updated += 1
                    else:
                        # Try insert as last resort
                        result = supabase.table("cars").insert(row).execute()
                        if result.data:
                            saved += 1
                        else:
                            errors += 1
                except Exception as fallback_err:
                    logger.error(f"[db] Fallback save failed: {fallback_err}")
                    errors += 1

        except Exception as e:
            errors += 1
            logger.error(
                f"[db] Помилка збереження {item.make} {item.model} "
                f"({item.source_site}): {type(e).__name__}: {e}"
            )

    logger.success(
        f"[db] Результат: {saved} нових, {updated} оновлено, {errors} помилок "
        f"(всього {len(items)} записів)"
    )
    return saved, updated, errors


def _build_update(car: ParsedCar, expires_at: str) -> dict:
    """
    Будує dict для UPDATE існуючого запису.
    Оновлює ВСЕ що має значення — не пропускає поля.
    Логіка: нове значення перезаписує старе, ЯКЩО нове не порожнє/None.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Завжди оновлюємо
    data: dict = {
        "parsed_at": now,
        "expires_at": expires_at,
    }

    # Оновлюємо якщо є значення (не None, не порожній рядок)
    _set_if_present(data, "make", car.make)
    _set_if_present(data, "model", car.model)
    _set_if_present(data, "year", car.year)
    _set_if_present(data, "price", car.price_eur)
    _set_if_present(data, "mileage", car.mileage_km)
    _set_if_present(data, "fuel", car.fuel)
    _set_if_present(data, "fuel_ua", car.fuel_ua)
    _set_if_present(data, "transmission", car.transmission)
    _set_if_present(data, "horsepower", car.horsepower)
    _set_if_present(data, "engine", car.engine)
    _set_if_present(data, "engine_cc", car.engine_cc)
    _set_if_present(data, "vin", car.vin)
    _set_if_present(data, "image", car.image)

    # Drive, body, color — оновлюємо якщо не "Unknown"
    if car.drive and car.drive != "Unknown":
        data["drive"] = car.drive
    if car.body_type and car.body_type != "Unknown":
        data["body_type"] = car.body_type
        data["body_type_ua"] = car.body_type_ua or car.body_type
    if car.color and car.color != "Unknown":
        data["color"] = car.color
        data["color_ua"] = car.color_ua or car.color

    # Числові поля
    if car.doors is not None:
        data["doors"] = car.doors
    if car.seats is not None:
        data["seats"] = car.seats

    # Списки — оновлюємо якщо не порожні
    if car.gallery:
        data["gallery"] = car.gallery
    if car.safety_features:
        data["safety_features"] = car.safety_features
    if car.comfort_features:
        data["comfort_features"] = car.comfort_features
    if car.infotainment:
        data["infotainment"] = car.infotainment
    if car.features_other:
        data["features_ua"] = car.features_other

    return data


def _set_if_present(data: dict, key: str, value) -> None:
    """Додає значення в dict якщо воно не None і не порожній рядок."""
    if value is not None and value != "":
        data[key] = value


# ══════════════════════════════════════════════════════════════════════════════
#  CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

def cleanup_expired(supabase: Client, source_type: str) -> int:
    """
    Видаляє протерміновані записи.
    Повертає кількість видалених.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        result = (
            supabase.table("cars")
            .delete()
            .eq("source_type", source_type)
            .lt("expires_at", now)
            .execute()
        )
        count = len(result.data) if result.data else 0
        if count:
            logger.info(f"[db] Видалено {count} протермінованих записів ({source_type})")
        return count
    except Exception as e:
        logger.warning(f"[db] Помилка очищення: {e}")
        return 0


def count_fresh_by_template(
    supabase: Client,
    template_id: str,
    max_age_hours: int = 6,
) -> int:
    """
    Перевіряє скільки свіжих записів є для даного шаблону.
    Для cache layer — якщо > 0, можна віддати кеш.
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        result = (
            supabase.table("cars")
            .select("id", count="exact")
            .eq("client_order_id", template_id)
            .gt("parsed_at", cutoff)
            .execute()
        )
        return result.count or 0
    except Exception as e:
        logger.warning(f"[db] Помилка підрахунку кешу: {e}")
        return 0