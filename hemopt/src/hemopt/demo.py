"""Demo engine that runs the real optimiser against a simulated house.

Spot prices are fetched live, so the schedule reacts to the actual Nordpool
curve for the configured price area. Only the Home Assistant side is
simulated: room temperatures, tank temperature and household load come from a
lightweight physical model instead of a recorder database.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from .config import (
    BaseLoadConfig,
    Config,
    HeatPumpConfig,
    HomeAssistantConfig,
    HotWaterConfig,
    MqttConfig,
    OptimiserConfig,
    PeakTariffConfig,
    RoomConfig,
    SiteConfig,
)
from .engine import Engine
from .ha import StatePoint
from .storage import Store
from .thermal import ThermalModel

DEMO_ROOMS = [
    ("vardagsrum", "Vardagsrum", "Övervåning", 1, 21.0, 22.5, 2.2),
    ("sovrum", "Sovrum", "Övervåning", 1, 19.0, 20.5, 1.4),
    ("badrum", "Badrum", "Övervåning", 1, 22.0, 23.5, 1.0),
    ("kontor", "Kontor", "Övervåning", 2, 20.0, 22.0, 1.2),
    ("pysselrum", "Pysselrum", "Övervåning", 2, 19.0, 21.5, 1.0),
    ("salong", "Salong", "Bottenvåning", 1, 20.5, 22.0, 1.6),
    ("lekrum", "Lekrum", "Bottenvåning", 2, 20.0, 22.0, 1.3),
    ("entre", "Entré", "Bottenvåning", 3, 18.0, 21.0, 0.9),
    ("tvattstuga", "Tvättstuga", "Bottenvåning", 3, 18.0, 21.0, 0.8),
]


def demo_config(database_path: str = ":memory:") -> Config:
    rooms = [
        RoomConfig(
            key=key,
            name=name,
            floor=floor,
            priority=priority,
            temperature_entity=f"sensor.demo_{key}_temperature",
            climate_entity=f"climate.demo_{key}",
            comfort_min=comfort_min,
            comfort_max=comfort_max,
            setpoint_min=comfort_min - 2.0,
            setpoint_max=comfort_max + 2.0,
            max_preheat_offset=1.5,
            max_setback_offset=1.5,
            heat_share=share,
        )
        for key, name, floor, priority, comfort_min, comfort_max, share in DEMO_ROOMS
    ]

    return Config(
        site=SiteConfig(price_area="SE3", main_fuse_amps=20),
        peak_tariff=PeakTariffConfig(
            enabled=True,
            n_peaks=5,
            price_per_kw_sek=67.5,
            # The live demo should show the peak logic working whatever month
            # it is run in, so the seasonal restriction is lifted here.
            window={"months": list(range(1, 13)), "hour_start": 7, "hour_end": 21},
        ),
        heat_pump=HeatPumpConfig(
            max_thermal_kw=9.0,
            max_electrical_kw=3.2,
            power_entity="sensor.demo_heat_pump_power",
            outdoor_entity="sensor.demo_outdoor",
        ),
        hot_water=HotWaterConfig(
            top_temperature_entity="sensor.demo_tank_top",
            tank_litres=180.0,
            reheat_power_kw=3.0,
        ),
        base_load=BaseLoadConfig(total_power_entity="sensor.demo_total_power", default_kw=0.7),
        rooms=rooms,
        home_assistant=HomeAssistantConfig(base_url="http://demo.invalid", token="demo"),
        mqtt=MqttConfig(enabled=False),
        optimiser=OptimiserConfig(
            horizon_hours=36, step_minutes=15, run_interval_minutes=5, apply_controls=True
        ),
        database_path=database_path,
    )


class DemoEngine(Engine):
    """Engine wired to a simulated house instead of Home Assistant."""

    def __init__(self, config: Config | None = None, store: Store | None = None) -> None:
        config = config or demo_config()
        super().__init__(config, store=store or Store(config.database_path))
        self._random = random.Random(20260914)
        self._temperatures = {
            room.key: (room.comfort_min + room.comfort_max) / 2.0 for room in config.rooms
        }
        self._tank = 51.0
        self.status.home_assistant_online = True
        self.status.control_enabled = True
        self._seed_models()
        self._seed_peak_history()

    def _seed_models(self) -> None:
        """Give each room a plausible identified model.

        Heavy concrete slabs downstairs, lighter construction upstairs, which
        is what makes the pre-heating behaviour visibly different per room.
        """
        for room in self.config.rooms:
            heavy = room.floor == "Bottenvåning"
            model = ThermalModel(
                tau_hours=115.0 if heavy else 78.0,
                k_heat_per_hour=0.62 if heavy else 0.95,
                k_gain_per_hour=0.01,
                r_squared=0.93 if heavy else 0.89,
                samples=2016,
                fitted=True,
            )
            self.models[room.key] = model
            self.store.save_thermal_model(room.key, model)

    def _seed_peak_history(self) -> None:
        """Populate the month with measured hourly peaks.

        Without history the optimiser has no threshold to defend and the peak
        constraint would be invisible in the demo.
        """
        now = self._now()
        day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while day < now:
            if day.weekday() < 5:
                for hour in (7, 8, 17, 18, 19):
                    moment = day.replace(hour=hour)
                    if moment >= now:
                        continue
                    value = 3.4 + self._random.uniform(-0.8, 1.4)
                    self.store.record_hourly_power(moment, value)
            day += timedelta(days=1)

    def _simulate(self, moment: datetime) -> dict[str, str]:
        outdoor = self._outdoor_at(moment)
        states: dict[str, str] = {"sensor.demo_outdoor": f"{outdoor:.1f}"}

        pump_kw = 0.0
        if self.plan is not None:
            index = self.plan.step_at(moment)
            pump_kw = self.plan.heat_pump_kw[index]
            for room_plan in self.plan.rooms:
                target = room_plan.temperature[index]
                current = self._temperatures[room_plan.key]
                # Track the plan with a lag so the chart shows the house
                # responding rather than teleporting to the setpoint.
                self._temperatures[room_plan.key] = current + 0.35 * (target - current)
            if self.plan.hot_water is not None:
                self._tank = self.plan.hot_water.temperature[index]

        for room in self.config.rooms:
            noise = self._random.uniform(-0.06, 0.06)
            states[room.temperature_entity] = f"{self._temperatures[room.key] + noise:.2f}"

        states["sensor.demo_tank_top"] = f"{self._tank:.1f}"
        states["sensor.demo_heat_pump_power"] = f"{pump_kw:.3f}"
        states["sensor.demo_total_power"] = f"{pump_kw + self._base_load_at(moment):.3f}"
        return states

    def _outdoor_at(self, moment: datetime) -> float:
        """Cold winter day with the usual diurnal swing."""
        hour = moment.hour + moment.minute / 60.0
        return -6.0 + 4.0 * math.sin((hour - 9.0) / 24.0 * 2 * math.pi)

    def _base_load_at(self, moment: datetime) -> float:
        hour = moment.hour
        if 6 <= hour < 9:
            base = 1.6
        elif 16 <= hour < 21:
            base = 2.1
        elif 21 <= hour < 23:
            base = 1.0
        else:
            base = 0.45
        return base + self._random.uniform(-0.1, 0.2)

    async def current_states(self) -> dict[str, str]:
        self.status.home_assistant_online = True
        return self._simulate(self._now())

    async def outdoor_forecast(self, states: dict[str, str], times: list[datetime]) -> list[float]:
        self.status.forecast_available = True
        return [self._outdoor_at(moment) for moment in times]

    async def fetch_history(self, start: datetime) -> dict[str, list[StatePoint]]:
        """Synthesise recorder history so training has something to chew on."""
        history: dict[str, list[StatePoint]] = {}
        step = timedelta(minutes=15)
        now = self._now()

        outdoor_points = []
        moment = start
        while moment < now:
            outdoor_points.append(StatePoint(moment, self._outdoor_at(moment)))
            moment += step
        history["sensor.demo_outdoor"] = outdoor_points

        for room in self.config.rooms:
            points = []
            temperature = (room.comfort_min + room.comfort_max) / 2.0
            moment = start
            while moment < now:
                outdoor = self._outdoor_at(moment)
                model = self.models[room.key]
                demand = 1.0 if temperature < room.comfort_min + 0.3 else 0.0
                temperature = model.step(temperature, outdoor, demand, 0.25)
                points.append(StatePoint(moment, temperature))
                moment += step
            history[room.temperature_entity] = points

        total_points = []
        pump_points = []
        moment = start
        while moment < now:
            pump = 1.8 if moment.hour in (1, 2, 3, 4, 13, 14) else 0.4
            pump_points.append(StatePoint(moment, pump))
            total_points.append(StatePoint(moment, pump + self._base_load_at(moment)))
            moment += step
        history["sensor.demo_heat_pump_power"] = pump_points
        history["sensor.demo_total_power"] = total_points

        tank_points = []
        temperature = 52.0
        moment = start
        while moment < now:
            if moment.hour in (7, 19) and moment.minute < 30:
                temperature -= 3.2
            temperature = min(temperature + 0.35, 55.0)
            tank_points.append(StatePoint(moment, temperature))
            moment += step
        history["sensor.demo_tank_top"] = tank_points

        return history

    async def apply(self) -> int:
        """Nothing to actuate: the simulation follows the plan directly."""
        return len(self.config.rooms) if self.status.control_enabled else 0
