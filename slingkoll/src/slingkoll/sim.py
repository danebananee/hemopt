"""A simulated house with cross-wired floor loops, for the demo and the tests.

It speaks the same small interface as the Home Assistant client, so the whole
app can run against it: thermostats that behave like LK Arc (open the loop
below the setpoint, readings in tenths every five minutes), a heat pump whose
water temperature follows the weather and its room setpoint, pauses for hot
water, a concrete slab downstairs and timber on chipboard upstairs.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Stockholm")


@dataclass(slots=True)
class SimRoom:
    key: str
    name: str
    floor: int
    lag_h: float
    tau_h: float
    k_heat: float
    sun: float
    temp: float = 21.0
    slab: float = 0.4


@dataclass(slots=True)
class SimThermostat:
    entity_id: str
    name: str
    room: str
    loops: list[str]
    target: float = 21.0
    valve: bool = False
    reading: float = 21.0


@dataclass(slots=True)
class SimHouse:
    rooms: dict[str, SimRoom]
    thermostats: list[SimThermostat]
    extra_sensors: dict[str, str] = field(default_factory=dict)  # entity -> room
    t: float = 0.0
    hp_setpoint: float = 21.0
    supply: float = 30.0
    hemopt_on: bool = True
    rng: random.Random = field(default_factory=lambda: random.Random(7))
    readings: dict[str, float] = field(default_factory=dict)
    next_reading: float = 0.0

    def outdoor(self) -> float:
        hour = self.local_hour()
        return 6.0 + 4.0 * math.sin(2 * math.pi * (hour - 9) / 24)

    def local_hour(self) -> float:
        moment = datetime.fromtimestamp(self.t, TZ)
        return moment.hour + moment.minute / 60

    def step(self, seconds: float = 60.0) -> None:
        dt_h = seconds / 3600
        hour = self.local_hour()
        outdoor = self.outdoor()
        rooms = list(self.rooms.values())
        room_avg = sum(r.temp for r in rooms) / len(rooms)

        heating = outdoor < 16 or self.hp_setpoint >= 22
        hot_water = (hour % 6) < 0.5
        if heating and not hot_water:
            goal = room_avg + 6 + 0.9 * max(0.0, 18 - outdoor) + 2 * (self.hp_setpoint - 21)
        else:
            goal = room_avg + 1
        self.supply += (1 - math.exp(-dt_h / 0.25)) * (goal - self.supply)

        for thermo in self.thermostats:
            if thermo.reading < thermo.target - 0.1:
                thermo.valve = True
            elif thermo.reading > thermo.target + 0.1:
                thermo.valve = False

        drive = {key: 0.0 for key in self.rooms}
        for thermo in self.thermostats:
            if thermo.valve:
                for loop in thermo.loops:
                    drive[loop] += max(0.0, self.supply - self.rooms[loop].temp) / 10

        sun = max(0.0, math.sin(math.pi * (hour - 8) / 10)) if 8 <= hour <= 18 else 0.0
        floors: dict[int, list[float]] = {}
        for room in rooms:
            floors.setdefault(room.floor, []).append(room.temp)
        for room in rooms:
            room.slab += (1 - math.exp(-dt_h / room.lag_h)) * (drive[room.key] - room.slab)
            floor_avg = sum(floors[room.floor]) / len(floors[room.floor])
            change = (
                (outdoor - room.temp) / room.tau_h
                + room.k_heat * room.slab
                + room.sun * sun
                + 0.05
                + 0.08 * (floor_avg - room.temp)
            )
            room.temp += dt_h * change

        self.t += seconds
        if self.t >= self.next_reading:
            self.next_reading = self.t + 300
            for thermo in self.thermostats:
                thermo.reading = self._read(self.rooms[thermo.room].temp)
            for entity, room in self.extra_sensors.items():
                self.readings[entity] = self._read(self.rooms[room].temp)

    def _read(self, value: float) -> float:
        return round(value + self.rng.gauss(0, 0.03), 1)


def demo_house(start: float | None = None) -> SimHouse:
    """Eight thermostats and a hall, with a swap, a three-way mix-up and a stray loop."""
    rooms = [
        SimRoom("vardagsrum", "Vardagsrum", 0, 3.0, 55, 0.40, 0.10),
        SimRoom("kok", "Kök", 0, 3.0, 50, 0.45, 0.05),
        SimRoom("tvatt", "Tvättstuga", 0, 3.0, 45, 0.50, 0.0),
        SimRoom("sovrum_bv", "Sovrum BV", 0, 3.0, 50, 0.45, 0.0),
        SimRoom("hall", "Hall", 0, 3.0, 45, 0.45, 0.0),
        SimRoom("sovrum_1", "Sovrum 1", 1, 0.75, 40, 0.55, 0.08),
        SimRoom("sovrum_2", "Sovrum 2", 1, 0.75, 40, 0.55, 0.0),
        SimRoom("kontor", "Kontor", 1, 0.75, 40, 0.55, 0.12),
        SimRoom("lekrum", "Lekrum", 1, 0.75, 40, 0.55, 0.0),
    ]
    # The truth the app has to find: which loop each thermostat really drives.
    wiring = {
        "vardagsrum": ["vardagsrum"],
        "kok": ["tvatt"],
        "tvatt": ["kok"],
        "sovrum_bv": ["hall"],
        "sovrum_1": ["sovrum_2"],
        "sovrum_2": ["kontor"],
        "kontor": ["sovrum_1"],
        "lekrum": ["lekrum"],
    }
    by_key = {room.key: room for room in rooms}
    thermostats = [
        SimThermostat(
            entity_id=f"climate.{key}_thermostat",
            name=by_key[key].name,
            room=key,
            loops=loops,
        )
        for key, loops in wiring.items()
    ]
    house = SimHouse(
        rooms=by_key,
        thermostats=thermostats,
        extra_sensors={"sensor.hall_temperatur": "hall"},
        t=start if start is not None else time.time(),
    )
    return house


class SimHA:
    """The subset of Home Assistant the app uses, backed by a SimHouse."""

    def __init__(self, house: SimHouse, history_days: float = 0.0) -> None:
        self.house = house
        self.log: list[tuple[float, list[dict[str, Any]]]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        if history_days:
            self.house.t -= history_days * 86400
            self.advance(history_days * 86400)

    # --- time -------------------------------------------------------------
    def now(self) -> float:
        return self.house.t

    def advance(self, seconds: float) -> None:
        end = self.house.t + seconds
        while self.house.t < end:
            self.house.step(60.0)
            if int(self.house.t) % 300 < 60:
                self.log.append((self.house.t, self.states()))
        cutoff = self.house.t - 10 * 86400
        if self.log and self.log[0][0] < cutoff:
            self.log = [entry for entry in self.log if entry[0] >= cutoff]

    # --- Home Assistant interface ----------------------------------------
    def config(self) -> dict[str, Any]:
        return {"time_zone": "Europe/Stockholm", "location_name": "Demohuset"}

    def states(self) -> list[dict[str, Any]]:
        house = self.house
        out: list[dict[str, Any]] = []
        for thermo in house.thermostats:
            out.append(
                {
                    "entity_id": thermo.entity_id,
                    "state": "heat",
                    "attributes": {
                        "friendly_name": thermo.name,
                        "current_temperature": thermo.reading,
                        "temperature": thermo.target,
                        "hvac_action": "heating" if thermo.valve else "idle",
                    },
                }
            )
            slug = thermo.entity_id.split(".")[1].removesuffix("_thermostat")
            out.append(_sensor(f"sensor.{slug}_temperature", thermo.reading, f"{thermo.name} Temperature"))
        for entity, room in house.extra_sensors.items():
            value = house.readings.get(entity, round(house.rooms[room].temp, 1))
            out.append(_sensor(entity, value, f"{house.rooms[room].name} temperatur"))
        out.append(
            {
                "entity_id": "climate.h66_hproom_temp_setpoint",
                "state": "heat",
                "attributes": {
                    "friendly_name": "H66 Room temp setpoint",
                    "temperature": house.hp_setpoint,
                    "current_temperature": 21.0,
                },
            }
        )
        out.append(_sensor("sensor.h66_hpradiator_forward", round(house.supply, 1), "H66 Radiator forward"))
        out.append(_sensor("sensor.h66_hpoutdoor", round(house.outdoor(), 1), "H66 Outdoor"))
        out.append(_sensor("sensor.varmvatten_topp", 51.0, "Varmvatten topp"))
        out.append(
            {
                "entity_id": "switch.hemopt_control_enabled",
                "state": "on" if house.hemopt_on else "off",
                "attributes": {"friendly_name": "Styr värmen"},
            }
        )
        return out

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self.calls.append((domain, service, dict(data)))
        entity = data.get("entity_id")
        house = self.house
        if domain == "climate" and service == "set_temperature":
            value = max(5.0, min(30.0, float(data["temperature"])))
            if entity == "climate.h66_hproom_temp_setpoint":
                house.hp_setpoint = value
                return
            for thermo in house.thermostats:
                if thermo.entity_id == entity:
                    thermo.target = value
                    return
            raise KeyError(entity)
        if domain == "switch" and entity == "switch.hemopt_control_enabled":
            house.hemopt_on = service == "turn_on"
            return
        raise KeyError(f"{domain}.{service}")

    def history(
        self, entity_ids: list[str], start: float, end: float | None = None
    ) -> dict[str, list[tuple[float, str, dict[str, Any]]]]:
        wanted = set(entity_ids)
        out: dict[str, list[tuple[float, str, dict[str, Any]]]] = {e: [] for e in entity_ids}
        for moment, states in self.log:
            if moment < start or (end is not None and moment > end):
                continue
            for state in states:
                if state["entity_id"] in wanted:
                    out[state["entity_id"]].append((moment, state["state"], state["attributes"]))
        return out


def _sensor(entity: str, value: float, name: str) -> dict[str, Any]:
    return {
        "entity_id": entity,
        "state": str(value),
        "attributes": {
            "friendly_name": name,
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
        },
    }
