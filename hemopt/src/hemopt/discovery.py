"""Find what a house has, from Home Assistant's own list of entities.

A new user should not have to know an entity id. This module looks at every
entity once and sorts them into the roles hemopt can use: room thermostats
and their temperature sensors, the heat pump's outdoor and power sensors, the
hot water tank, the electricity meter and its per-phase currents, the
weather, a home battery, an electric car and its charger. Every list is
ordered best guess first, so the setup guide can preselect the top entry and
the user only has to confirm.

Matching is on names and on what Home Assistant says about each entity (its
device class and unit), in Swedish and English, because integrations name
things in either.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .ha import parse_numeric
from .meters import rank_power_meter, suggest_total_power_entities


@dataclass(frozen=True, slots=True)
class Candidate:
    entity_id: str
    name: str
    state: str
    unit: str = ""
    score: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "state": self.state,
            "unit": self.unit,
        }


@dataclass(slots=True)
class RoomSuggestion:
    name: str
    climate_entity: str | None
    temperature_entity: str | None
    humidity_entity: str | None = None
    floor: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "climate_entity": self.climate_entity,
            "temperature_entity": self.temperature_entity,
            "humidity_entity": self.humidity_entity,
            "floor": self.floor,
        }


@dataclass(slots=True)
class Discovery:
    rooms: list[RoomSuggestion] = field(default_factory=list)
    roles: dict[str, list[Candidate]] = field(default_factory=dict)
    phase_currents: list[str] = field(default_factory=list)
    location_known: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "rooms": [room.as_dict() for room in self.rooms],
            "roles": {
                role: [candidate.as_dict() for candidate in candidates]
                for role, candidates in self.roles.items()
            },
            "phase_currents": self.phase_currents,
        }


def _fold(text: str) -> str:
    """Lower case without diacritics, so 'Utomhus' and 'utomhus' and 'ute' match."""
    normalised = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalised if not unicodedata.combining(c)).lower()


def _words(row: dict) -> str:
    attrs = row.get("attributes") or {}
    return _fold(f"{row.get('entity_id', '')} {attrs.get('friendly_name', '')}")


def _candidate(row: dict, score: int = 0) -> Candidate:
    attrs = row.get("attributes") or {}
    return Candidate(
        entity_id=row["entity_id"],
        name=str(attrs.get("friendly_name") or row["entity_id"]),
        state=str(row.get("state", "")),
        unit=str(attrs.get("unit_of_measurement") or ""),
        score=score,
    )


def _domain(row: dict) -> str:
    return str(row.get("entity_id", "")).split(".", 1)[0]


def _has(words: str, *needles: str) -> bool:
    return any(needle in words for needle in needles)


def _is_temperature(row: dict) -> bool:
    attrs = row.get("attributes") or {}
    return _domain(row) == "sensor" and (
        attrs.get("device_class") == "temperature"
        or attrs.get("unit_of_measurement") in {"°C", "C", "°F"}
    )


def _is_numeric(row: dict) -> bool:
    return parse_numeric(str(row.get("state"))) is not None


# Name fragments per role. Order in each list does not matter; the score does.
_OUTDOOR = ("outdoor", "outside", "utomhus", "ute_", "_ute", "utetemp", "hpoutdoor", "exterior")
_HEAT_PUMP = (
    "heat_pump",
    "heatpump",
    "varmepump",
    "hp_",
    "_hp",
    "compressor",
    "kompressor",
    "h66",
    "h60",
    "nibe",
    "ivt",
    "thermia",
)
_HOT_WATER = ("warm_water", "hot_water", "varmvatten", "dhw", "tank", "beredare", "tappvatten")
_CAR = (
    "car",
    "bil",
    "tesla",
    "volvo",
    "polestar",
    "kia",
    "hyundai",
    "ev_",
    "_ev",
    "vehicle",
    "id.",
    "id3",
    "id4",
    "ioniq",
    "leaf",
    "enyaq",
)
_CHARGER = (
    "charger",
    "laddbox",
    "laddare",
    "easee",
    "zaptec",
    "wallbox",
    "charge_point",
    "chargepoint",
    "ctek",
    "garo",
    "go_e",
    "goe",
    "evse",
)
_HOME_BATTERY = (
    "home_battery",
    "house_battery",
    "husbatteri",
    "villabatteri",
    "powerwall",
    "sungrow",
    "huawei",
    "solaredge",
    "sonnen",
    "ferroamp",
    "battery_storage",
    "batterilager",
    "storage",
)
_PHONEISH = (
    "phone",
    "iphone",
    "pixel",
    "galaxy",
    "watch",
    "tablet",
    "ipad",
    "remote",
    "sensor_battery",
    "_battery_level",
    "door",
    "window",
    "motion",
    "leak",
)
_PHASE = re.compile(r"(?:^|[_ ])(l|phase_?|fas_?)([123])(?:$|[_ ])")


def discover(rows: list[dict], diagnosis: dict | None = None) -> Discovery:
    """Sort every entity into the roles hemopt understands."""
    by_id = {row["entity_id"]: row for row in rows if "entity_id" in row}
    result = Discovery(location_known=bool(diagnosis and "latitude" in diagnosis))

    temperatures = [row for row in rows if _is_temperature(row) and _is_numeric(row)]
    humidities = [
        row
        for row in rows
        if _domain(row) == "sensor"
        and (row.get("attributes") or {}).get("device_class") == "humidity"
    ]

    # Rooms: every thermostat that looks like a room, paired with the
    # temperature sensor that shares its device prefix.
    for row in rows:
        if _domain(row) != "climate":
            continue
        words = _words(row)
        if _has(words, "ext_control", "ext_port", "hpext", "setpoint", "hot_water", "varmvatten"):
            continue
        entity = row["entity_id"]
        stem = entity.split(".", 1)[1].removesuffix("_thermostat").removesuffix("_climate")
        temperature = next(
            (
                t["entity_id"]
                for t in temperatures
                if t["entity_id"].split(".", 1)[1].startswith(stem)
            ),
            None,
        )
        humidity = next(
            (
                h["entity_id"]
                for h in humidities
                if h["entity_id"].split(".", 1)[1].startswith(stem)
            ),
            None,
        )
        name = str((row.get("attributes") or {}).get("friendly_name") or stem)
        name = re.sub(r"\s*(thermostat|termostat|climate)\s*$", "", name, flags=re.IGNORECASE)
        result.rooms.append(
            RoomSuggestion(
                name=name.strip() or stem,
                climate_entity=entity,
                temperature_entity=temperature,
                humidity_entity=humidity,
            )
        )

    def ranked(filtered: list[tuple[int, dict]]) -> list[Candidate]:
        ordered = sorted(filtered, key=lambda item: (-item[0], item[1]["entity_id"]))
        return [_candidate(row, score) for score, row in ordered]

    roles: dict[str, list[Candidate]] = {}

    roles["temperature"] = ranked([(0, row) for row in temperatures])

    roles["outdoor"] = ranked(
        [
            (2 if _has(_words(row), "hpoutdoor", "utomhus", "outdoor") else 1, row)
            for row in temperatures
            if _has(_words(row), *_OUTDOOR)
        ]
    )

    power_rows = [
        row
        for row in rows
        if _domain(row) == "sensor"
        and _is_numeric(row)
        and (
            (row.get("attributes") or {}).get("device_class") == "power"
            or (row.get("attributes") or {}).get("unit_of_measurement") in {"W", "kW"}
        )
    ]
    roles["heat_pump_power"] = ranked(
        [
            (2 if _has(_words(row), "consumption", "forbrukning") else 1, row)
            for row in power_rows
            if _has(_words(row), *_HEAT_PUMP)
        ]
    )

    roles["hot_water_temperature"] = ranked(
        [
            (2 if _has(_words(row), "top", "topp") else 1, row)
            for row in temperatures
            if _has(_words(row), *_HOT_WATER)
        ]
    )
    roles["hot_water_setpoint"] = ranked(
        [
            (1, row)
            for row in rows
            if _domain(row) in {"number", "water_heater", "climate"}
            and _has(_words(row), *_HOT_WATER)
        ]
    )
    roles["house_setpoint"] = ranked(
        [
            (1, row)
            for row in rows
            if _domain(row) in {"climate", "number"}
            and _has(_words(row), *_HEAT_PUMP)
            and _has(_words(row), "room", "rum", "inne", "indoor", "setpoint")
        ]
    )

    states = {row["entity_id"]: str(row.get("state")) for row in rows}
    roles["meter"] = [
        _candidate(by_id[str(item["entity_id"])], 3 - rank_power_meter(str(item["entity_id"]))[0])
        for item in suggest_total_power_entities(states)
        if str(item["entity_id"]) in by_id
    ]

    roles["weather"] = ranked([(1, row) for row in rows if _domain(row) == "weather"])

    # Per-phase current: amperes with an L1/L2/L3 marker, grouped by device.
    currents = [
        row
        for row in rows
        if _domain(row) == "sensor"
        and _is_numeric(row)
        and (row.get("attributes") or {}).get("unit_of_measurement") == "A"
        and _PHASE.search(_fold(row["entity_id"].split(".", 1)[1]))
    ]
    groups: dict[str, list[str]] = {}
    for row in currents:
        key = _PHASE.sub(" ", _fold(row["entity_id"].split(".", 1)[1])).strip()
        groups.setdefault(key, []).append(row["entity_id"])
    complete = [sorted(ids) for ids in groups.values() if len(ids) in (1, 3)]
    complete.sort(key=lambda ids: (-len(ids), 0 if _has(ids[0], "p1", "meter") else 1))
    result.phase_currents = complete[0] if complete else []

    soc_rows = [
        row
        for row in rows
        if _domain(row) == "sensor"
        and _is_numeric(row)
        and (
            (row.get("attributes") or {}).get("device_class") == "battery"
            or (row.get("attributes") or {}).get("unit_of_measurement") == "%"
        )
        and not _has(_words(row), *_PHONEISH)
    ]
    roles["battery_soc"] = ranked(
        [
            (2 if _has(_words(row), *_HOME_BATTERY) else 1, row)
            for row in soc_rows
            if _has(_words(row), "battery", "batteri", "soc", "state_of_charge")
            and not _has(_words(row), *_CAR)
        ]
    )
    roles["battery_power"] = ranked(
        [(1, row) for row in power_rows if _has(_words(row), *_HOME_BATTERY, "battery_power")]
    )
    roles["battery_control"] = ranked(
        [
            (2 if _has(_words(row), "charge", "ladd", "power", "setpoint") else 1, row)
            for row in rows
            if _domain(row) in {"number", "select"}
            and _has(_words(row), *_HOME_BATTERY, "battery", "batteri")
        ]
    )
    roles["ev_soc"] = ranked(
        [
            (2 if _has(_words(row), "battery", "batteri", "soc", "charge_level") else 1, row)
            for row in rows
            if _domain(row) == "sensor"
            and _is_numeric(row)
            and (row.get("attributes") or {}).get("unit_of_measurement") == "%"
            and _has(_words(row), *_CAR)
        ]
    )
    roles["ev_plugged"] = ranked(
        [
            (2 if _has(_words(row), "plug", "cable", "kabel", "connected", "ansluten") else 1, row)
            for row in rows
            if _domain(row) in {"binary_sensor", "sensor"}
            and _has(_words(row), *_CAR, *_CHARGER)
            and _has(_words(row), "plug", "cable", "kabel", "connected", "ansluten", "status")
        ]
    )
    roles["ev_charger_control"] = ranked(
        [
            (2 if _domain(row) == "number" else 1, row)
            for row in rows
            if _domain(row) in {"number", "switch"}
            and _has(_words(row), *_CHARGER, "charging", "laddning")
        ]
    )
    roles["ev_charger_power"] = ranked(
        [(1, row) for row in power_rows if _has(_words(row), *_CHARGER, *_CAR)]
    )
    roles["stove_temperature"] = ranked(
        [
            (1, row)
            for row in temperatures
            if _has(
                _words(row), "kamin", "brasa", "stove", "fireplace", "eldstad", "rokgas", "flue"
            )
        ]
    )

    result.roles = roles
    return result


def room_key(name: str, taken: set[str]) -> str:
    """A stable, readable key for a room name: 'Rios rum' -> 'rios_rum'."""
    base = re.sub(r"[^a-z0-9]+", "_", _fold(name)).strip("_") or "rum"
    key, index = base, 2
    while key in taken:
        key = f"{base}_{index}"
        index += 1
    taken.add(key)
    return key
