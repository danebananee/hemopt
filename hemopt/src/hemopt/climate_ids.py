"""Resolve LK Arc climate entity ids (…_thermostat vs legacy bare MAC).

hemopt.yaml / older profiles sometimes still point at climate.<mac> while
lk_arc_climate pins climate.<mac>_thermostat. Prefer the id that actually
exists in Home Assistant, and derive a candidate from the temperature sensor.
"""

from __future__ import annotations

from typing import Any


def climate_entity_candidates(
    climate_entity: str | None, temperature_entity: str | None = None
) -> list[str]:
    """Ordered guesses for a room's writable climate entity."""
    candidates: list[str] = []

    def add(entity_id: str | None) -> None:
        if entity_id and entity_id not in candidates:
            candidates.append(entity_id)

    add(climate_entity)
    if climate_entity and climate_entity.startswith("climate."):
        if climate_entity.endswith("_thermostat"):
            add(climate_entity[: -len("_thermostat")])
        else:
            add(f"{climate_entity}_thermostat")

    if temperature_entity and temperature_entity.startswith("sensor."):
        object_id = temperature_entity.split(".", 1)[1]
        if object_id.endswith("_temperature"):
            slug = object_id[: -len("_temperature")]
            add(f"climate.{slug}_thermostat")
            add(f"climate.{slug}")

    return candidates


def resolve_climate_entity(
    climate_entity: str | None,
    states: dict[str, Any] | set[str] | list[str],
    *,
    temperature_entity: str | None = None,
) -> str | None:
    """Return the first candidate that exists in `states`, else None."""
    known = states if isinstance(states, (set, dict)) else set(states)
    for candidate in climate_entity_candidates(climate_entity, temperature_entity):
        if candidate in known:
            return candidate
    return None


def repair_room_climate_entities(
    rooms: list[Any], states: dict[str, Any] | set[str] | list[str]
) -> list[dict[str, str]]:
    """Rewrite room.climate_entity in place when a better live id is found."""
    repaired: list[dict[str, str]] = []
    for room in rooms:
        resolved = resolve_climate_entity(
            getattr(room, "climate_entity", None),
            states,
            temperature_entity=getattr(room, "temperature_entity", None),
        )
        current = getattr(room, "climate_entity", None)
        if resolved and resolved != current:
            room.climate_entity = resolved
            repaired.append(
                {
                    "key": getattr(room, "key", ""),
                    "from": current or "",
                    "to": resolved,
                }
            )
    return repaired
