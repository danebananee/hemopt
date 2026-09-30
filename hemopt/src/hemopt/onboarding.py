"""The setup guide: what is configured, what is missing, and saving changes.

Everything a household would otherwise write into hemopt.yaml can be set from
the panel. The guide shows one checklist; each item says in plain words what
it is for and whether it is done. Saving merges only the fields the guide
knows about into the configuration, so hand-made settings elsewhere survive.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .discovery import room_key

# Fields the guide may change, per section. Anything else is left alone.
EDITABLE: dict[str, tuple[str, ...]] = {
    "site": (
        "price_area",
        "main_fuse_amps",
        "phases",
        "weather_entity",
        "phase_current_entities",
    ),
    "energy_price": ("contract",),
    "heat_pump": ("outdoor_entity", "power_entity", "room_setpoint_entity"),
    "hot_water": ("enabled", "top_temperature_entity", "setpoint_entity"),
    "base_load": ("total_power_entity",),
    "wood_stove": ("name", "temperature_entity", "binary_entity"),
    "battery": (
        "enabled",
        "name",
        "capacity_kwh",
        "max_charge_kw",
        "max_discharge_kw",
        "min_soc_pct",
        "soc_entity",
        "power_entity",
        "control_entity",
        "control_unit",
    ),
    "ev": (
        "enabled",
        "name",
        "battery_kwh",
        "max_charge_kw",
        "charger_phases",
        "max_charge_amps",
        "departure_time",
        "target_soc_pct",
        "soc_entity",
        "plugged_entity",
        "charger_control_entity",
        "charger_power_entity",
        "v2h_enabled",
        "v2h_max_discharge_kw",
        "v2h_min_soc_pct",
        "v2h_control_entity",
    ),
}

ROOM_FIELDS = (
    "key",
    "name",
    "floor",
    "floor_type",
    "priority",
    "temperature_entity",
    "humidity_entity",
    "climate_entity",
    "comfort_min",
    "comfort_max",
    "heat_share",
)

# Settings the add-on's Configuration tab also carries. Once set here, the
# panel owns them.
PANEL_MANAGED = {
    ("site", "price_area"): "site.price_area",
    ("site", "weather_entity"): "site.weather_entity",
    ("energy_price", "contract"): "energy_price.contract",
    ("base_load", "total_power_entity"): "base_load.total_power_entity",
}


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def apply_setup(config: Config, payload: dict[str, Any]) -> Config:
    """A new configuration with the guide's changes merged in.

    Raises ValueError (from validation) when the result is not a valid
    configuration, so nothing half-saved ever reaches the engine.
    """
    data = config.model_dump(mode="json")
    managed = set(config.panel_managed)

    for section, fields in EDITABLE.items():
        changes = payload.get(section)
        if not isinstance(changes, dict):
            continue
        for name in fields:
            if name in changes:
                data[section][name] = _blank_to_none(changes[name])
                if (section, name) in PANEL_MANAGED:
                    managed.add(PANEL_MANAGED[(section, name)])

    if isinstance(payload.get("rooms"), list):
        existing = {room["key"]: room for room in data["rooms"]}
        taken: set[str] = set()
        rooms = []
        for raw in payload["rooms"]:
            if not isinstance(raw, dict) or not str(raw.get("name", "")).strip():
                continue
            if not raw.get("temperature_entity"):
                continue
            key = raw.get("key") if raw.get("key") in existing else None
            if key is None or key in taken:
                key = room_key(str(raw["name"]), taken | set(existing) - {key})
            taken.add(key)
            room = dict(existing.get(key, {}))
            for name in ROOM_FIELDS:
                if name in raw:
                    room[name] = _blank_to_none(raw[name])
            room["key"] = key
            room.setdefault("heat_share", 1.0)
            if room.get("heat_share") is None:
                room["heat_share"] = 1.0
            rooms.append(room)
        data["rooms"] = rooms

    data["panel_managed"] = sorted(managed)
    return Config.model_validate(data)


def checklist(config: Config, *, meter_found: bool = True) -> list[dict[str, Any]]:
    """What the guide shows as done or missing, in the order it is set up."""
    items: list[dict[str, Any]] = []

    def item(key: str, title: str, done: bool, detail: str, required: bool = True) -> None:
        items.append(
            {"key": key, "title": title, "done": done, "detail": detail, "required": required}
        )

    item(
        "basics",
        "Elområde, elavtal och säkring",
        "site.price_area" in config.panel_managed or bool(config.rooms),
        "Styr vilka spotpriser som gäller och hur huset debiteras.",
    )
    item(
        "rooms",
        "Rum",
        bool(config.rooms),
        f"{len(config.rooms)} rum inlagda."
        if config.rooms
        else "Minst ett rum med en temperaturgivare behövs för att planera värmen.",
    )
    item(
        "heat_pump",
        "Värmepump",
        bool(config.heat_pump.outdoor_entity),
        "Utetemperaturen är viktigast; effektmätning gör planen och besparingen noggrannare.",
    )
    item(
        "meter",
        "Elmätare",
        bool(config.base_load.total_power_entity),
        "Hela husets effekt, för effekttoppar, besparing och säkringsbevakning."
        if meter_found
        else "Ingen elmätare hittades i Home Assistant. Den behövs inte men ger mer.",
        required=False,
    )
    item(
        "weather",
        "Väderprognos",
        bool(config.site.weather_entity),
        "Förvärmning inför kyla och mindre värme inför soliga dagar.",
        required=False,
    )
    item(
        "hot_water",
        "Varmvatten",
        bool(config.hot_water.enabled and config.hot_water.top_temperature_entity),
        "Planerar laddningen av varmvattnet till billiga timmar.",
        required=False,
    )
    item(
        "battery",
        "Husbatteri",
        config.battery.enabled,
        "Laddar när elen är billig och används när den är dyr.",
        required=False,
    )
    item(
        "ev",
        "Elbil",
        config.ev.enabled,
        "Laddar till avgångstiden på billigaste timmarna, och kan hjälpa huset (V2H).",
        required=False,
    )
    return items


def is_complete(config: Config) -> bool:
    """Enough to plan: at least one room and an outdoor temperature."""
    return bool(config.rooms) and bool(config.heat_pump.outdoor_entity)
