"""Configuration schema for the cost optimiser.

Everything the optimiser needs to know about the house, the electricity
contract and the grid tariff lives here. Units are normalised on load so the
rest of the code only ever sees kW, kWh, SEK/kWh and degrees Celsius.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

PriceArea = Literal["SE1", "SE2", "SE3", "SE4"]

# How the spot price is settled. The grid fee and the tax are the same either
# way; what differs is how finely the energy part is resolved, and therefore
# how much there is to gain from shifting load within a day.
Contract = Literal["fixed", "daily", "hourly", "quarterly"]


def data_dir() -> Path:
    """Where persistent state lives.

    Inside the add-on this is /data, which survives updates. Elsewhere it is
    the working directory, so a checkout behaves like any other Python tool.
    """
    return Path(os.environ.get("HEMOPT_DATA") or ".")


def profile_path() -> Path:
    return data_dir() / "profile.json"


class SiteConfig(BaseModel):
    price_area: PriceArea = "SE3"
    timezone: str = "Europe/Stockholm"
    main_fuse_amps: float = 20.0
    voltage: float = 230.0
    phases: int = 3
    # Weather entity supplying the hourly outdoor forecast. Without one the
    # planner has to assume the current temperature holds for 36 hours, which
    # systematically mis-sizes pre-heating ahead of a cold snap.
    weather_entity: str | None = None

    @property
    def fuse_limit_kw(self) -> float:
        return self.main_fuse_amps * self.voltage * self.phases / 1000.0


class EnergyPriceConfig(BaseModel):
    """Everything added on top of the raw spot price.

    The spot feed is quoted excluding VAT, so `vat_rate` is applied to the
    spot price and to every ore/kWh adder that is configured net of VAT.
    """

    contract: Contract = "hourly"
    vat_rate: float = 0.25
    supplier_markup_ore: float = 8.0
    certificate_ore: float = 0.0
    energy_tax_ore: float = 43.9
    transfer_fee_high_ore: float = 31.12
    transfer_fee_normal_ore: float = 12.4
    prices_include_vat: bool = False
    # What a fixed-price contract costs, used as the comparison baseline when
    # simulating what the other contract types would have cost.
    fixed_price_ore: float = 85.0

    def adder_sek_per_kwh(self, high_load: bool) -> float:
        transfer = self.transfer_fee_high_ore if high_load else self.transfer_fee_normal_ore
        ore = self.supplier_markup_ore + self.certificate_ore + self.energy_tax_ore + transfer
        sek = ore / 100.0
        return sek if self.prices_include_vat else sek * (1.0 + self.vat_rate)


class PeakWindow(BaseModel):
    """When the grid operator actually measures peaks.

    Defaults follow the Vattenfall Eldistribution model: non-holiday weekdays
    07-21 during November-March. Outside the window peaks are free, which the
    optimiser exploits by shifting load into the night and the weekend.
    """

    months: list[int] = Field(default_factory=lambda: [1, 2, 3, 11, 12])
    hour_start: int = 7
    hour_end: int = 21
    weekdays_only: bool = True
    exclude_holidays: bool = True


class PeakTariffConfig(BaseModel):
    """Peak (effektavgift) part of the grid bill.

    `n_peaks` peaks measured on distinct days are averaged over the month and
    charged at `price_per_kw_sek`. Vattenfall uses 5; some operators use 3 and
    others bill only the single highest hour, which is `n_peaks = 1`.
    """

    enabled: bool = True
    n_peaks: int = 5
    price_per_kw_sek: float = 67.5
    measurement_minutes: int = 60
    window: PeakWindow = Field(default_factory=PeakWindow)
    prices_include_vat: bool = True
    vat_rate: float = 0.25

    @property
    def effective_price_per_kw(self) -> float:
        if self.prices_include_vat:
            return self.price_per_kw_sek
        return self.price_per_kw_sek * (1.0 + self.vat_rate)

    @property
    def marginal_price_per_kw(self) -> float:
        """Cost of raising the monthly peak average by one kW.

        Replacing the lowest counted peak with one that is 1 kW higher lifts
        the average by 1/n, so that is what a new peak actually costs.
        """
        return self.effective_price_per_kw / max(self.n_peaks, 1)


class RoomConfig(BaseModel):
    """One heated zone.

    `priority` is the only knob most users touch: it scales the penalty for
    letting the room drift from its comfort band, so high-priority rooms keep
    their temperature while low-priority rooms absorb the load shifting.
    """

    key: str
    name: str
    floor: str = ""
    priority: int = Field(default=3, ge=1, le=5)
    temperature_entity: str
    humidity_entity: str | None = None
    climate_entity: str | None = None
    comfort_min: float = 20.0
    comfort_max: float = 23.0
    setpoint_min: float = 18.0
    setpoint_max: float = 24.0
    max_preheat_offset: float = 1.5
    max_setback_offset: float = 1.5
    heat_share: float = Field(default=1.0, gt=0.0)

    @property
    def comfort_weight(self) -> float:
        """SEK charged per degree-hour outside the comfort band.

        Priority 5 is expensive enough that the solver will never trade the
        room away for spot-price savings; priority 1 is cheap enough that it
        is the first to coast.
        """
        return {1: 0.5, 2: 1.5, 3: 4.0, 4: 12.0, 5: 40.0}[self.priority]


class HotWaterConfig(BaseModel):
    enabled: bool = True
    top_temperature_entity: str | None = None
    setpoint_entity: str | None = None
    tank_litres: float = 180.0
    min_temperature: float = 42.0
    target_temperature: float = 52.0
    max_temperature: float = 58.0
    reheat_power_kw: float = 3.0
    standing_loss_kwh_per_day: float = 1.2
    comfort_weight: float = 25.0
    legionella_weekday: int | None = 6
    legionella_temperature: float = 62.0

    @property
    def kwh_per_degree(self) -> float:
        return self.tank_litres * 4.186 / 3600.0


class HeatPumpConfig(BaseModel):
    """Heat pump capability and efficiency.

    COP is modelled as a linear function of outdoor temperature, which tracks
    a ground-source unit closely enough over the -20..+15 C range that matters
    for scheduling.
    """

    max_thermal_kw: float = 8.0
    max_electrical_kw: float = 3.0
    cop_at_minus_5: float = 3.1
    cop_at_plus_10: float = 4.6
    cop_hot_water_penalty: float = 0.75
    min_cop: float = 1.6
    aux_heater_kw: float = 6.0
    power_entity: str | None = None
    outdoor_entity: str | None = None
    # The three-way valve serves either heating or the tank, but over a
    # 15-minute step it can split between them, so a shared capacity limit is
    # the accurate model. Forcing a hard per-step choice adds one binary per
    # step and roughly ten times the solve time for no physical gain.
    strict_dhw_interlock: bool = False

    def cop(self, outdoor_c: float, hot_water: bool = False) -> float:
        slope = (self.cop_at_plus_10 - self.cop_at_minus_5) / 15.0
        value = self.cop_at_minus_5 + slope * (outdoor_c - (-5.0))
        if hot_water:
            value *= self.cop_hot_water_penalty
        return max(value, self.min_cop)


class BaseLoadConfig(BaseModel):
    """Household consumption the optimiser cannot control but must plan around."""

    total_power_entity: str | None = None
    default_kw: float = 0.6
    learn_profile: bool = True


class ExtControlConfig(BaseModel):
    """Hardware block via the heat pump's external input.

    Setpoints are advisory: a thermostat can always decide to call for heat
    anyway. An EXT input is not, which makes it the only reliable way to hold
    a peak-hour ceiling. What each port blocks is set in the heat pump, so
    nothing here is enabled by default.
    """

    enabled: bool = False
    block_heating_entity: str | None = None
    block_hot_water_entity: str | None = None
    # Guard rails, because an EXT block stops the compressor outright.
    max_block_minutes: int = 120
    min_release_minutes: int = 15
    min_room_temperature: float = 18.0


class HomeAssistantConfig(BaseModel):
    base_url: str = "http://homeassistant.local:8123"
    token: str = ""
    verify_ssl: bool = True
    history_days: int = 21


class MqttConfig(BaseModel):
    enabled: bool = True
    host: str = "homeassistant.local"
    port: int = 1883
    # None rather than "" so an anonymous broker is expressible, which is what
    # the Supervisor reports when Mosquitto runs without authentication.
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"
    node_id: str = "hemopt"


class OptimiserConfig(BaseModel):
    horizon_hours: int = 36
    step_minutes: int = 15
    solver_time_limit_s: float = 30.0
    mip_gap: float = 0.01
    run_interval_minutes: int = 15
    apply_controls: bool = False
    # Cost charged per unit of step-to-step change in a room's heat output.
    # A pure cost minimum is bang-bang, which would make the thermostats jitter
    # every quarter hour; this buys smooth setpoints for a negligible amount of
    # money. Raise it if the valves still hunt, drop it to zero to see the
    # unconstrained economic optimum.
    move_penalty_sek: float = 0.08

    @property
    def steps(self) -> int:
        return int(self.horizon_hours * 60 / self.step_minutes)

    @property
    def step_hours(self) -> float:
        return self.step_minutes / 60.0

    @model_validator(mode="after")
    def _check_step(self) -> OptimiserConfig:
        if 60 % self.step_minutes != 0:
            raise ValueError("step_minutes must divide 60")
        return self


class Config(BaseModel):
    site: SiteConfig = Field(default_factory=SiteConfig)
    energy_price: EnergyPriceConfig = Field(default_factory=EnergyPriceConfig)
    peak_tariff: PeakTariffConfig = Field(default_factory=PeakTariffConfig)
    heat_pump: HeatPumpConfig = Field(default_factory=HeatPumpConfig)
    hot_water: HotWaterConfig = Field(default_factory=HotWaterConfig)
    base_load: BaseLoadConfig = Field(default_factory=BaseLoadConfig)
    ext_control: ExtControlConfig = Field(default_factory=ExtControlConfig)
    rooms: list[RoomConfig] = Field(default_factory=list)
    home_assistant: HomeAssistantConfig = Field(default_factory=HomeAssistantConfig)
    mqtt: MqttConfig = Field(default_factory=MqttConfig)
    optimiser: OptimiserConfig = Field(default_factory=OptimiserConfig)
    database_path: str = "hemopt.db"

    @model_validator(mode="after")
    def _unique_room_keys(self) -> Config:
        keys = [room.key for room in self.rooms]
        duplicates = {key for key in keys if keys.count(key) > 1}
        if duplicates:
            raise ValueError(f"duplicate room keys: {sorted(duplicates)}")
        return self

    def room(self, key: str) -> RoomConfig:
        for room in self.rooms:
            if room.key == key:
                return room
        raise KeyError(key)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)

    @classmethod
    def resolve(cls, path: str | Path | None = None) -> Config:
        """Build the effective config from a file, a saved profile and the env.

        Running as a Home Assistant add-on means nobody edits a YAML file: the
        Supervisor hands over the API token and the broker credentials, and the
        household profile is written by the setup UI. An explicit path still
        wins so a hand-written config keeps working.
        """
        if path is not None:
            config = cls.load(path)
        elif (profile := profile_path()).exists():
            config = cls.model_validate_json(profile.read_text(encoding="utf-8"))
        else:
            config = cls()

        return config.with_environment()

    def with_environment(self) -> Config:
        """Overlay the environment on top of this config.

        Connection details are taken from the environment unconditionally
        because under the Supervisor they are rotated for us, and a stale copy
        saved in a profile would silently break after an add-on restart.
        """
        data = self.model_dump(mode="json")

        if url := os.environ.get("HEMOPT_HA_URL"):
            data["home_assistant"]["base_url"] = url
        if token := os.environ.get("HEMOPT_HA_TOKEN"):
            data["home_assistant"]["token"] = token

        if host := os.environ.get("HEMOPT_MQTT_HOST"):
            data["mqtt"]["enabled"] = True
            data["mqtt"]["host"] = host
            data["mqtt"]["port"] = int(os.environ.get("HEMOPT_MQTT_PORT") or 1883)
            data["mqtt"]["username"] = os.environ.get("HEMOPT_MQTT_USERNAME") or None
            data["mqtt"]["password"] = os.environ.get("HEMOPT_MQTT_PASSWORD") or None

        if area := os.environ.get("HEMOPT_PRICE_AREA"):
            data["site"]["price_area"] = area
        if contract := os.environ.get("HEMOPT_CONTRACT"):
            data["energy_price"]["contract"] = contract

        if data_dir := os.environ.get("HEMOPT_DATA"):
            data["database_path"] = str(Path(data_dir) / "hemopt.db")

        return Config.model_validate(data)

    def save_profile(self) -> Path:
        """Persist the household profile where the add-on will find it again.

        Only the household's own description is stored. Credentials are left
        out on purpose: they come from the Supervisor on every start, and
        writing them to disk would turn a backup into a secret leak.
        """
        data = self.model_dump(mode="json")
        data["home_assistant"]["token"] = ""
        data["mqtt"]["username"] = None
        data["mqtt"]["password"] = None

        path = profile_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def save(self, path: str | Path) -> None:
        data = self.model_dump(mode="json", exclude_defaults=False)
        Path(path).write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
