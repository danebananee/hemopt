"""Find thermostats, room sensors and the heat pump among Home Assistant's entities."""

from __future__ import annotations

from typing import Any

from .ha import number
from .settings import Settings

HEAT_PUMP_WORDS = ("h66", "heat_pump", "heatpump", "varmepump", "värmepump", "rego", "nibe", "ivt")
NOT_ROOM_WORDS = (
    "outdoor",
    "outside",
    "ute",
    "utomhus",
    "water",
    "vatten",
    "tank",
    "brine",
    "forward",
    "return",
    "supply",
    "framledning",
    "retur",
    "cpu",
    "battery",
    "batteri",
    "processor",
    "hot_gas",
    "exhaust",
    "frys",
    "freezer",
    "kyl",
    "fridge",
    "weather",
    "dew",
    "dagg",
)


def _name(state: dict[str, Any]) -> str:
    return str((state.get("attributes") or {}).get("friendly_name") or state["entity_id"])


def _is_room_thermostat(state: dict[str, Any]) -> bool:
    entity = state["entity_id"]
    attrs = state.get("attributes") or {}
    if not entity.startswith("climate."):
        return False
    if any(word in entity.lower() for word in HEAT_PUMP_WORDS):
        return False
    return "temperature" in attrs and "current_temperature" in attrs


def _is_temperature_sensor(state: dict[str, Any]) -> bool:
    entity = state["entity_id"]
    attrs = state.get("attributes") or {}
    if not entity.startswith("sensor."):
        return False
    if attrs.get("device_class") != "temperature" and attrs.get("unit_of_measurement") not in {"°C", "°F"}:
        return False
    return number(state.get("state")) is not None


def _slug(entity: str) -> str:
    return entity.split(".", 1)[1].removesuffix("_thermostat")


def discover(states: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything the setup page can offer, best candidates first."""
    thermostats = [s for s in states if _is_room_thermostat(s)]
    lk = [s for s in thermostats if s["entity_id"].endswith("_thermostat")]
    # LK Arc Climate names its entities <mac>_thermostat. When there are
    # such, other climate entities are almost certainly something else.
    chosen = lk or thermostats
    thermo_slugs = [_slug(s["entity_id"]) for s in thermostats]

    sensors = []
    for state in states:
        if not _is_temperature_sensor(state):
            continue
        entity = state["entity_id"].lower()
        name = _name(state).lower()
        if any(slug and slug in entity for slug in thermo_slugs):
            continue  # the thermostat's own reading, already covered
        if any(word in entity or word in name for word in NOT_ROOM_WORDS + HEAT_PUMP_WORDS):
            continue
        sensors.append(state)

    def pick(domain_prefixes: tuple[str, ...], words: tuple[str, ...]) -> list[dict[str, str]]:
        found = []
        for word in words:
            for state in states:
                entity = state["entity_id"]
                if not entity.startswith(domain_prefixes):
                    continue
                if word in entity.lower() or word in _name(state).lower():
                    item = {"entity_id": entity, "name": _name(state)}
                    if item not in found:
                        found.append(item)
        return found

    return {
        "thermostats": [
            {"entity_id": s["entity_id"], "name": _name(s), "default": s in chosen} for s in thermostats
        ],
        "sensors": [{"entity_id": s["entity_id"], "name": _name(s)} for s in sensors],
        "hp_setpoint": pick(
            ("climate.", "number."),
            ("room_temp_setpoint", "room_setpoint", "rumsbörvärde", "rum_borvarde"),
        ),
        "supply": pick(
            ("sensor.",),
            ("radiator_forward", "heat_carrier_forw", "framledning", "supply_temp", "flow_temp", "forward"),
        ),
        "outdoor": pick(("sensor.",), ("hpoutdoor", "outdoor", "utomhus", "utetemp", "ute_temp")),
        "hemopt_switch": pick(("switch.",), ("hemopt_control_enabled",)),
    }


def default_settings(found: dict[str, Any], base: Settings | None = None) -> Settings:
    """First-run settings: every LK thermostat, and the heat pump if found."""
    settings = base or Settings()
    settings.thermostats = [
        {"entity_id": t["entity_id"], "name": t["name"]} for t in found["thermostats"] if t["default"]
    ]
    for key, attr in (
        ("hp_setpoint", "hp_setpoint_entity"),
        ("supply", "supply_entity"),
        ("outdoor", "outdoor_entity"),
        ("hemopt_switch", "hemopt_switch_entity"),
    ):
        if found[key] and not getattr(settings, attr):
            setattr(settings, attr, found[key][0]["entity_id"])
    return settings
