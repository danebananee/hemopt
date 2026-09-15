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
# how much there is to gain from shifting load at all.
#
# "monthly" is what Vattenfall sells as "Rorligt Elpris": every kWh in the
# month is billed at that month's mean spot. Under it, moving load from an
# expensive hour to a cheap one saves exactly nothing, which is the first
# thing the advice engine looks for.
Contract = Literal["fixed", "monthly", "daily", "hourly", "quarterly"]

CONTRACT_NAMES: dict[str, str] = {
    "fixed": "Fastpris",
    "monthly": "Rorligt, manadsmedel",
    "daily": "Rorligt, dygnsmedel",
    "hourly": "Rorligt, timpris",
    "quarterly": "Rorligt, kvartspris",
}


def data_dir() -> Path:
    """Where persistent state lives.

    Inside the add-on this is /data, which survives updates. Elsewhere it is
    the working directory, so a checkout behaves like any other Python tool.
    """
    return Path(os.environ.get("HEMOPT_DATA") or ".")


def profile_path() -> Path:
    return data_dir() / "profile.json"


def house_config_candidates() -> list[Path]:
    """YAML files a user can drop in for the household description.

    Checked before the saved profile so a hand-edited file wins after an
    update. `/homeassistant` is Home Assistant's config directory when the
    add-on maps `homeassistant_config`; `/config` is the add-on's own folder.
    """
    return [
        Path("/homeassistant/hemopt.yaml"),
        Path("/config/hemopt.yaml"),
        data_dir() / "hemopt.yaml",
    ]


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
    supplier_markup_ore: float = 7.0
    certificate_ore: float = 1.4
    # Pass-through balancing and profile costs. Billed per kWh but restated
    # every month, so it is kept apart from the fixed markup you agreed to.
    balancing_ore: float = 5.76
    energy_tax_ore: float = 36.0
    # Equal values mean a single-rate grid tariff (enkeltariff), where nothing
    # is gained on the grid side by moving load off peak hours.
    transfer_fee_high_ore: float = 35.6
    transfer_fee_normal_ore: float = 35.6
    prices_include_vat: bool = False
    # What a fixed-price contract costs, used as the comparison baseline when
    # simulating what the other contract types would have cost.
    fixed_price_ore: float = 85.0
    # Standing charges. They do not change with consumption, but they do change
    # when you switch fuse size or supplier, which is what the advice engine
    # exists to spot.
    grid_subscription_sek_per_year: float = 0.0
    supplier_fee_sek_per_year: float = 0.0

    @property
    def variable_adder_ore(self) -> float:
        """Everything charged per kWh on top of spot, excluding VAT."""
        return (
            self.supplier_markup_ore
            + self.certificate_ore
            + self.balancing_ore
            + self.energy_tax_ore
        )

    @property
    def is_single_rate_grid(self) -> bool:
        return self.transfer_fee_high_ore == self.transfer_fee_normal_ore

    def adder_sek_per_kwh(self, high_load: bool) -> float:
        transfer = self.transfer_fee_high_ore if high_load else self.transfer_fee_normal_ore
        sek = (self.variable_adder_ore + transfer) / 100.0
        return sek if self.prices_include_vat else sek * (1.0 + self.vat_rate)

    def fixed_sek_per_year(self) -> float:
        total = self.grid_subscription_sek_per_year + self.supplier_fee_sek_per_year
        return total if self.prices_include_vat else total * (1.0 + self.vat_rate)


class GridTariffOption(BaseModel):
    """One grid tariff you could be on, including the one you already have.

    Grid tariffs are chosen, not negotiated: the operator publishes a table and
    you pick a fuse size and a rate type. That makes them worth comparing
    against measured load, because the only thing standing between you and a
    cheaper row is whether your peaks actually fit under it.
    """

    name: str
    fuse_amps: int
    subscription_sek_per_year: float
    transfer_ore: float
    # Set both for a time-of-use tariff; leave them out for a single-rate one.
    transfer_high_ore: float | None = None
    transfer_normal_ore: float | None = None
    peak_price_per_kw_sek: float = 0.0

    def capacity_kw(self, voltage: float, phases: int) -> float:
        return self.fuse_amps * voltage * phases / 1000.0


class SupplierOffer(BaseModel):
    """A retail offer to compare your current one against."""

    name: str
    contract: Contract = "hourly"
    markup_ore: float = 0.0
    certificate_ore: float = 0.0
    yearly_fee_sek: float = 0.0


class AdviceConfig(BaseModel):
    """What the advice engine is allowed to suggest, and how boldly."""

    enabled: bool = True
    # Below this, a recommendation is noise rather than advice.
    min_annual_saving_sek: float = 200.0
    # How much headroom a smaller fuse must keep over the largest peak seen.
    # A blown main fuse in January is worth more than the subscription saved.
    fuse_margin_kw: float = 2.0
    # Peaks are only trustworthy once there is enough measured history.
    min_history_days: int = 30
    # Roughly how much of a year's consumption the optimiser can move in time.
    # In a heat-pumped house the pump and the hot water tank are most of it;
    # cooking, lighting and laundry are not. Used only when estimating what a
    # finer-grained contract would be worth.
    flexible_share: float = 0.5
    grid_tariffs: list[GridTariffOption] = Field(default_factory=list)
    supplier_offers: list[SupplierOffer] = Field(default_factory=list)


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
    advice: AdviceConfig = Field(default_factory=AdviceConfig)
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

        Running as a Home Assistant add-on, the Supervisor hands over the API
        token and broker credentials. The household description (rooms, meter,
        peak tariff details beyond the simple options) comes from a YAML file
        next to ``configuration.yaml``, or from the saved profile.
        """
        if path is not None:
            config = cls.load(path)
        else:
            config = None
            for candidate in house_config_candidates():
                if candidate.exists():
                    config = cls.load(candidate)
                    break
            if config is None and (profile := profile_path()).exists():
                config = cls.model_validate_json(profile.read_text(encoding="utf-8"))
            if config is None:
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

        if "HEMOPT_PEAK_ENABLED" in os.environ:
            data["peak_tariff"]["enabled"] = os.environ["HEMOPT_PEAK_ENABLED"].lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        if n_peaks := os.environ.get("HEMOPT_PEAK_N"):
            data["peak_tariff"]["n_peaks"] = int(n_peaks)
        if price := os.environ.get("HEMOPT_PEAK_PRICE"):
            data["peak_tariff"]["price_per_kw_sek"] = float(price)
        if hour_start := os.environ.get("HEMOPT_PEAK_HOUR_START"):
            data["peak_tariff"]["window"]["hour_start"] = int(hour_start)
        if hour_end := os.environ.get("HEMOPT_PEAK_HOUR_END"):
            data["peak_tariff"]["window"]["hour_end"] = int(hour_end)
        if "HEMOPT_PEAK_WEEKDAYS" in os.environ:
            data["peak_tariff"]["window"]["weekdays_only"] = os.environ[
                "HEMOPT_PEAK_WEEKDAYS"
            ].lower() in {"1", "true", "yes", "on"}

        if entity := os.environ.get("HEMOPT_TOTAL_POWER_ENTITY"):
            entity = entity.strip()
            if entity:
                data["base_load"]["total_power_entity"] = entity

        if data_dir_env := os.environ.get("HEMOPT_DATA"):
            data["database_path"] = str(Path(data_dir_env) / "hemopt.db")

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
