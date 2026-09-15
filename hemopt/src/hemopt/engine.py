"""Orchestration: collect, learn, plan, apply, publish.

The engine owns all mutable state and runs three loops at different cadences:
sampling every minute so the live peak guard stays accurate, planning every
few minutes, and model training once a night when the house is quiet.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import baseload
from .advice import AdviceReport, build_advice, samples_from_hourly
from .config import Config
from .explain import explain_plan, explain_upcoming, headline
from .guard import GuardDecision, PeakGuard
from .ha import HomeAssistantClient, parse_numeric, resample_forecast
from .hotwater import TankSample, UsageProfile, build_profile, estimate_draws
from .mqtt_bridge import MqttBridge
from .optimizer import (
    HotWaterInput,
    InfeasiblePlan,
    OptimisationInput,
    PeakInput,
    Plan,
    RoomInput,
    solve,
    solve_baseline,
)
from .peaks import HourAccumulator, PeakState, peak_state
from .prices import PriceClient, PriceSeries
from .storage import Store
from .thermal import ThermalModel, ThermalSample, identify
from .timeutil import floor_to_step, is_peak_window
from .woodstove import (
    WoodStoveEffect,
    WoodStoveReading,
    WoodStoveReport,
    build_report,
    detect_lit,
    recommend_windows,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class EngineStatus:
    """Everything the web UI and MQTT bridge need to describe the system."""

    last_sample: datetime | None = None
    last_plan: datetime | None = None
    last_training: datetime | None = None
    last_advice: datetime | None = None
    home_assistant_online: bool = False
    mqtt_online: bool = False
    prices_available: bool = False
    forecast_available: bool = False
    control_enabled: bool = False
    errors: list[str] = field(default_factory=list)
    ha_diagnosis: dict | None = None


class Engine:
    def __init__(self, config: Config, store: Store | None = None) -> None:
        self.config = config
        self.tz = ZoneInfo(config.site.timezone)
        self.store = store or Store(config.database_path)
        self.status = EngineStatus(control_enabled=config.optimiser.apply_controls)

        self.plan: Plan | None = None
        self.baseline: Plan | None = None
        self.peaks: PeakState | None = None
        self.prices: PriceSeries | None = None
        self.models: dict[str, ThermalModel] = {}
        self.hot_water_profile: UsageProfile = UsageProfile.default()
        self.base_load = baseload.BaseLoadProfile(fallback_kw=config.base_load.default_kw)
        self.accumulator = HourAccumulator(
            hour_start=self._now().replace(minute=0, second=0, microsecond=0)
        )
        self.guard = PeakGuard(config.ext_control)
        self.guard_decision = GuardDecision(False, "not evaluated")
        self.advice = AdviceReport()
        self.wood_stove = WoodStoveReport()
        self._wood_stove_lit = False
        self._ext_state: dict[str, bool] = {}

        self._mqtt: MqttBridge | None = None
        self._http: httpx.AsyncClient | None = None
        self._ha_http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._load_persisted()

    # --- lifecycle -------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _load_persisted(self) -> None:
        for room in self.config.rooms:
            stored = self.store.thermal_model(room.key)
            self.models[room.key] = stored or ThermalModel.default()
            priority = self.store.setting(f"priority_{room.key}")
            if isinstance(priority, int) and 1 <= priority <= 5:
                # Legacy 4–5 meant "hold"; that is priority 1 on the new scale.
                room.priority = 1 if priority >= 4 else min(priority, 3)
            comfort = self.store.setting(f"comfort_{room.key}")
            if isinstance(comfort, dict):
                lo = comfort.get("min")
                hi = comfort.get("max")
                if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo < hi:
                    room.comfort_min = float(lo)
                    room.comfort_max = float(hi)

        profile = self.store.hot_water_profile()
        if profile is not None:
            self.hot_water_profile = profile

        stored_control = self.store.setting("control_enabled")
        if isinstance(stored_control, bool):
            self.status.control_enabled = stored_control

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=30.0)
        # Dedicated client for Home Assistant so base URL and token live on the
        # connection itself. Reusing the price-feed client without a base URL
        # used to turn every /api call into a relative path and look like an
        # outage.
        ha = self.config.home_assistant
        from .ha import normalize_ha_base_url

        base = normalize_ha_base_url(ha.base_url)
        self._ha_http = httpx.AsyncClient(
            base_url=base,
            headers={"Authorization": f"Bearer {ha.token}"},
            verify=ha.verify_ssl,
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=True,
        )
        if self.config.mqtt.enabled:
            self._mqtt = MqttBridge(self.config, on_command=self._handle_command)
            try:
                self._mqtt.connect()
            except OSError as exc:
                _LOGGER.warning("MQTT unavailable: %s", exc)
                self._mqtt = None

    async def stop(self) -> None:
        if self._mqtt is not None:
            self._mqtt.disconnect()
            self._mqtt = None
        if self._ha_http is not None:
            await self._ha_http.aclose()
            self._ha_http = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _handle_command(self, key: str, payload: str) -> None:
        if key == "control_enabled":
            enabled = payload.strip().lower() in {"true", "on", "1"}
            self.status.control_enabled = enabled
            self.store.set_setting("control_enabled", enabled)
            _LOGGER.info("control %s via MQTT", "enabled" if enabled else "disabled")
            return

        if key.startswith("priority_"):
            room_key = key.removeprefix("priority_")
            try:
                value = int(float(payload))
            except ValueError:
                return
            value = max(1, min(5, value))
            try:
                self.config.room(room_key).priority = value
            except KeyError:
                return
            self.store.set_setting(f"priority_{room_key}", value)
            _LOGGER.info("priority for %s set to %d", room_key, value)

    # --- data collection --------------------------------------------------
    def _tracked_entities(self) -> list[str]:
        entities: list[str] = []
        for room in self.config.rooms:
            entities.append(room.temperature_entity)
            if room.humidity_entity:
                entities.append(room.humidity_entity)
            if room.climate_entity:
                entities.append(room.climate_entity)
        for candidate in (
            self.config.heat_pump.power_entity,
            self.config.heat_pump.outdoor_entity,
            self.config.hot_water.top_temperature_entity,
            self.config.base_load.total_power_entity,
            self.config.wood_stove.temperature_entity,
            self.config.wood_stove.binary_entity,
        ):
            if candidate:
                entities.append(candidate)
        return list(dict.fromkeys(entities))

    async def current_states(self) -> dict[str, str] | None:
        """Read every tracked entity. Overridden by the demo engine."""
        async with HomeAssistantClient(self.config.home_assistant, self._ha_http) as ha:
            diagnosis = await ha.diagnose()
            self.status.ha_diagnosis = diagnosis
            online = bool(diagnosis.get("ok"))
            self.status.home_assistant_online = online
            if not online:
                return None
            return await ha.states()

    async def fetch_history(self, start: datetime) -> dict[str, list]:
        async with HomeAssistantClient(self.config.home_assistant, self._ha_http) as ha:
            if not await ha.ping():
                return {}
            return await ha.history(self._tracked_entities(), start)

    async def collect(self) -> None:
        """Sample Home Assistant and keep the live peak accumulator fed."""
        states = await self.current_states()
        if states is None:
            return

        now = self._now()
        rows = []
        for entity_id in self._tracked_entities():
            value = parse_numeric(states.get(entity_id))
            if value is not None:
                rows.append((entity_id, now, value))
        self.store.record_samples(rows)
        self.status.last_sample = now
        self._update_wood_stove_reading(states, now)

        total_entity = self.config.base_load.total_power_entity
        total_kw = parse_numeric(states.get(total_entity)) if total_entity else None
        if total_kw is None and self.config.heat_pump.power_entity:
            total_kw = parse_numeric(states.get(self.config.heat_pump.power_entity))
        if total_kw is None:
            return

        # Home Assistant power sensors are usually watts; kilowatts are what
        # the tariff is billed in.
        if total_kw > 100:
            total_kw /= 1000.0

        previous_hour = self.accumulator.hour_start
        self.accumulator.add_sample(now, total_kw)
        if self.accumulator.hour_start != previous_hour:
            self.store.record_hourly_power(previous_hour, self.accumulator.energy_kwh)

        self.store.record_hourly_power(
            self.accumulator.hour_start,
            self.accumulator.projected_hour_kw(now, total_kw),
        )

        await self._run_guard(now, states)

    async def _run_guard(self, now: datetime, states: dict[str, str]) -> None:
        """Re-evaluate the live peak guard and drive the EXT input."""
        pump_kw = 0.0
        if self.config.heat_pump.power_entity:
            value = parse_numeric(states.get(self.config.heat_pump.power_entity))
            if value is not None:
                pump_kw = value / 1000.0 if value > 100 else value

        temperatures = [
            parse_numeric(states.get(room.temperature_entity)) for room in self.config.rooms
        ]
        measured = [value for value in temperatures if value is not None]

        self.guard_decision = self.guard.evaluate(
            now=now,
            accumulator=self.accumulator,
            threshold_kw=self.peaks.threshold_kw if self.peaks else 0.0,
            in_peak_window=is_peak_window(now, self.config.peak_tariff.window),
            heat_pump_kw=pump_kw,
            coldest_room_c=min(measured) if measured else None,
        )

        if self.status.control_enabled:
            await self._apply_ext_block(self.guard_decision.block)

    async def _apply_ext_block(self, block: bool) -> None:
        """Set the EXT input, but only when the desired state actually changes.

        Writing every minute would flood the heat pump's register bus and fill
        the Home Assistant logbook for no benefit.
        """
        ext = self.config.ext_control
        entity = ext.block_heating_entity
        if not ext.enabled or not entity:
            return
        if self._ext_state.get(entity) == block:
            return

        try:
            async with HomeAssistantClient(self.config.home_assistant, self._ha_http) as ha:
                await ha.set_ext_port(entity, block)
        except Exception as exc:  # noqa: BLE001 - never let actuation kill the loop
            self._record_error(f"EXT block: {exc}")
            return

        self._ext_state[entity] = block
        _LOGGER.info("EXT heating block %s", "engaged" if block else "released")

    # --- learning ---------------------------------------------------------
    async def train(self) -> None:
        """Refit the thermal models, hot water profile and base load."""
        history_days = self.config.home_assistant.history_days
        start = self._now() - timedelta(days=history_days)
        history = await self.fetch_history(start)
        if not history:
            return

        outdoor_entity = self.config.heat_pump.outdoor_entity
        outdoor = history.get(outdoor_entity, []) if outdoor_entity else []
        outdoor_series = {point.moment: point.value for point in outdoor}
        outdoor_times = sorted(outdoor_series)

        stove_on_by_time, stove_times = self._stove_history_series(history)
        hours_lit = 0.0
        sessions = 0
        if stove_times:
            prev = False
            for moment in stove_times:
                on = stove_on_by_time.get(moment, 0.0) >= 0.5
                if on:
                    hours_lit += 0.25  # history is irregular; approx refined below
                if on and not prev:
                    sessions += 1
                prev = on
            # Better estimate from consecutive deltas.
            hours_lit = 0.0
            for earlier, later in zip(stove_times, stove_times[1:], strict=False):
                if stove_on_by_time.get(earlier, 0.0) >= 0.5:
                    hours_lit += max((later - earlier).total_seconds() / 3600.0, 0.0)
            self.store.set_setting(
                "wood_stove_stats",
                {"sessions": sessions, "hours_lit": round(hours_lit, 2)},
            )

        stove_rooms = set(self.config.wood_stove.room_keys)

        for room in self.config.rooms:
            indoor = history.get(room.temperature_entity, [])
            if len(indoor) < 100 or not outdoor_times:
                continue

            climate = history.get(room.climate_entity, []) if room.climate_entity else []
            climate_by_time = {point.moment: point.value for point in climate}
            climate_times = sorted(climate_by_time)

            samples = []
            for point in indoor:
                nearest_outdoor = _nearest_value(outdoor_series, outdoor_times, point.moment)
                if nearest_outdoor is None:
                    continue
                heat_fraction = _heat_fraction(
                    climate_by_time, climate_times, point.moment, point.value
                )
                stove_on = 0.0
                if room.key in stove_rooms and stove_times:
                    stove_val = _nearest_value(stove_on_by_time, stove_times, point.moment)
                    stove_on = 1.0 if stove_val is not None and stove_val >= 0.5 else 0.0
                samples.append(
                    ThermalSample(
                        moment=point.moment,
                        indoor=point.value,
                        outdoor=nearest_outdoor,
                        heat_fraction=heat_fraction,
                        stove_on=stove_on,
                    )
                )

            model = identify(samples, prior=self.models.get(room.key))
            if model.fitted:
                self.models[room.key] = model
                self.store.save_thermal_model(room.key, model)
                _LOGGER.info(
                    "%s: tau %.1f h, heat %.2f K/h, stove %.2f K/h, R2 %.2f",
                    room.name,
                    model.tau_hours,
                    model.k_heat_per_hour,
                    model.k_stove_per_hour,
                    model.r_squared,
                )

        tank_entity = self.config.hot_water.top_temperature_entity
        if tank_entity and tank_entity in history:
            pump_entity = self.config.heat_pump.power_entity
            pump_history = history.get(pump_entity, []) if pump_entity else []
            pump_by_time = {point.moment: point.value for point in pump_history}
            pump_times = sorted(pump_by_time)

            tank_samples = [
                TankSample(
                    moment=point.moment,
                    top_temperature=point.value,
                    charging=(
                        (_nearest_value(pump_by_time, pump_times, point.moment) or 0.0) > 0.3
                    ),
                )
                for point in history[tank_entity]
            ]
            events = estimate_draws(tank_samples, self.config.hot_water)
            self.hot_water_profile = build_profile(
                events, days_observed=history_days, prior=self.hot_water_profile
            )
            self.store.save_hot_water_profile(self.hot_water_profile)
            _LOGGER.info(
                "hot water: %d draws, %.1f kWh/day",
                len(events),
                self.hot_water_profile.daily_total_kwh(),
            )

        total_entity = self.config.base_load.total_power_entity
        if self.config.base_load.learn_profile and total_entity and total_entity in history:
            pump_entity = self.config.heat_pump.power_entity
            self.base_load = baseload.build_profile(
                total_power=[(p.moment, _to_kw(p.value)) for p in history[total_entity]],
                heat_pump_power=[
                    (p.moment, _to_kw(p.value)) for p in history.get(pump_entity or "", [])
                ],
                fallback_kw=self.config.base_load.default_kw,
            )
            _LOGGER.info("base load profile built from %d samples", self.base_load.samples)

        self.status.last_training = self._now()

    # --- planning ---------------------------------------------------------
    async def outdoor_forecast(self, states: dict[str, str], times: list[datetime]) -> list[float]:
        """Outdoor temperature per step over the whole horizon.

        A 36-hour plan built on a frozen current reading mis-sizes every
        pre-heat decision, so a weather entity is used when one is configured.
        Its first hour is nudged onto the heat pump's own outdoor sensor, which
        is the measurement the thermal models were trained against.
        """
        measured = None
        if self.config.heat_pump.outdoor_entity:
            measured = parse_numeric(states.get(self.config.heat_pump.outdoor_entity))

        entity = self.config.site.weather_entity
        if entity:
            async with HomeAssistantClient(self.config.home_assistant, self._ha_http) as ha:
                points = await ha.weather_forecast(entity)
            if points:
                series = resample_forecast(points, times, fallback=measured or 0.0)
                if measured is not None:
                    bias = measured - series[0]
                    # The forecast's own trend is trusted; only its offset from
                    # the sensor on the wall is corrected, decaying over 6 h.
                    decay = max(int(6 * 60 / self.config.optimiser.step_minutes), 1)
                    series = [
                        value + bias * max(0.0, 1.0 - index / decay)
                        for index, value in enumerate(series)
                    ]
                self.status.forecast_available = True
                return series

        self.status.forecast_available = False
        return [measured if measured is not None else 0.0] * len(times)

    def _peak_input(self, times: list[datetime], now: datetime) -> PeakInput:
        tariff = self.config.peak_tariff
        month = now.strftime("%Y-%m")
        hourly = self.store.hourly_power(month)
        expected = self.store.expected_peak_kw(month, fallback=self._default_expected_peak())
        state = peak_state(hourly, tariff, expected_peak_kw=expected)
        self.peaks = state

        # Persisting the running result is what lets next year's January start
        # with a realistic threshold instead of a guess.
        self.store.record_month_result(
            month=month,
            average_kw=state.average_kw,
            threshold_kw=state.threshold_kw,
            cost_sek=state.projected_cost_sek,
        )

        in_window = [is_peak_window(t, tariff.window) for t in times]
        marginal = tariff.marginal_price_per_kw if tariff.enabled else 0.0

        elapsed = (
            self.accumulator.energy_kwh
            if self.accumulator.hour_start == times[0].replace(minute=0, second=0, microsecond=0)
            else 0.0
        )

        return PeakInput(
            threshold_kw=state.threshold_kw,
            marginal_sek_per_kw=marginal,
            in_window=in_window,
            elapsed_energy_kwh=elapsed,
            elapsed_hours=(now - self.accumulator.hour_start).total_seconds() / 3600.0,
        )

    def _default_expected_peak(self) -> float:
        """Seed for the peak threshold before any month has been measured."""
        pump = self.config.heat_pump.max_electrical_kw
        return round(pump + self.config.base_load.default_kw * 2.0, 2)

    async def refresh_advice(self) -> AdviceReport:
        """Re-examine the contracts against everything measured so far.

        Kept separate from planning and run rarely: the answer changes on the
        timescale of seasons, and it needs historical spot prices, which means
        a burst of requests to the price feed.
        """
        history = self.store.all_hourly_power()
        if len(history) < 48:
            self.advice = AdviceReport(
                notes=["For lite matdata an; radgivningen behover minst tva dygn."]
            )
            return self.advice

        load = samples_from_hourly(history)
        spot = await self._historical_spot([sample.start for sample in load])
        if spot is None:
            self.advice = AdviceReport(notes=["Historiska spotpriser kunde inte hamtas."])
            return self.advice

        self.advice = build_advice(
            self.config,
            load,
            spot,
            peak_kw=max(history.values()) if history else None,
        )
        self.status.last_advice = self._now()
        return self.advice

    async def _historical_spot(self, times: list[datetime]) -> list[float] | None:
        """Published spot price for each measured hour."""
        async with PriceClient(
            self.config.site.price_area,
            self.config.energy_price,
            self.config.peak_tariff.window,
            self._http,
        ) as client:
            by_start: dict[datetime, float] = {}
            for day in sorted({moment.date() for moment in times}):
                points = await client.fetch_day(day)
                if not points:
                    continue
                for point in points:
                    by_start[point.start] = point.spot_sek_per_kwh

        if not by_start:
            return None

        # An hour with no published price keeps the previous one rather than
        # dropping the sample, so the load and price series stay aligned.
        spot, last = [], 0.0
        for moment in times:
            hour = moment.replace(minute=0, second=0, microsecond=0)
            last = by_start.get(hour, last)
            spot.append(last)
        return spot

    async def replan(self) -> Plan | None:
        async with self._lock:
            return await self._replan_locked()

    async def _replan_locked(self) -> Plan | None:
        settings = self.config.optimiser
        now = self._now()
        start = floor_to_step(now, settings.step_minutes)
        steps = settings.steps

        price_client = PriceClient(
            self.config.site.price_area,
            self.config.energy_price,
            self.config.peak_tariff.window,
            client=self._http,
        )
        try:
            prices = await price_client.series(start, steps, settings.step_minutes)
        except RuntimeError as exc:
            self.status.prices_available = False
            self._record_error(f"spot prices unavailable: {exc}")
            return None

        self.prices = prices
        self.status.prices_available = True

        states = await self.current_states()
        if states is None:
            self._record_error("Home Assistant unreachable, keeping previous plan")
            return None
        outdoor = await self.outdoor_forecast(states, prices.times)

        rooms: list[RoomInput] = []
        for room in self.config.rooms:
            current = parse_numeric(states.get(room.temperature_entity))
            if current is None:
                _LOGGER.warning("%s has no reading, skipping from plan", room.name)
                continue
            rooms.append(
                RoomInput(
                    config=room,
                    model=self.models.get(room.key, ThermalModel.default()),
                    initial_temperature=current,
                    nominal_heat_kw=self._nominal_heat_kw(room.heat_share),
                )
            )

        if not rooms:
            self._record_error("no room temperatures available")
            return None

        hot_water = self._hot_water_input(states, prices.times, prices.total)
        peak = self._peak_input(prices.times, now)

        problem = OptimisationInput(
            start=start,
            step_minutes=settings.step_minutes,
            times=prices.times,
            price_sek_per_kwh=prices.total,
            outdoor_c=outdoor,
            base_load_kw=self.base_load.series(start, steps, settings.step_minutes),
            rooms=rooms,
            heat_pump=self.config.heat_pump,
            peak=peak,
            fuse_limit_kw=self.config.site.fuse_limit_kw,
            hot_water=hot_water,
            solver_time_limit_s=settings.solver_time_limit_s,
            mip_gap=settings.mip_gap,
            move_penalty_sek=settings.move_penalty_sek,
        )

        try:
            plan = solve(problem)
        except InfeasiblePlan as exc:
            self._record_error(f"planning failed: {exc}")
            return None

        try:
            baseline_plan = solve_baseline(problem)
            plan.baseline_energy_cost_sek = baseline_plan.energy_cost_sek
            plan.baseline_peak_cost_sek = baseline_plan.peak_cost_sek
            self.baseline = baseline_plan
        except InfeasiblePlan:
            _LOGGER.debug("baseline comparison unavailable")

        if prices.is_partly_forecast:
            plan.notes.append(
                "Morgondagens spotpriser saknas, senare delen av planen bygger pa dagens profil."
            )

        self.plan = plan
        self.status.last_plan = now
        self.store.save_plan(now, plan_to_dict(plan))
        self.refresh_wood_stove()
        self._publish(plan, now)
        return plan

    def _update_wood_stove_reading(self, states: dict[str, str], now: datetime) -> None:
        cfg = self.config.wood_stove
        if not cfg.enabled:
            self.wood_stove = build_report(cfg, WoodStoveReading(), [], [])
            return
        binary = None
        if cfg.binary_entity:
            raw = parse_numeric(states.get(cfg.binary_entity))
            binary = None if raw is None else raw >= 0.5
        temp = None
        if cfg.temperature_entity:
            temp = parse_numeric(states.get(cfg.temperature_entity))
        reading = detect_lit(
            cfg,
            binary_on=binary,
            temperature_c=temp,
            previously_lit=self._wood_stove_lit,
        )
        reading.updated_at = now
        self._wood_stove_lit = reading.lit
        # Keep effects/windows from last refresh; only update live reading.
        self.wood_stove.reading = reading
        if reading.lit and self.wood_stove.status not in {"lit", "disabled"}:
            self.refresh_wood_stove()

    def _stove_history_series(
        self, history: dict[str, list]
    ) -> tuple[dict[datetime, float], list[datetime]]:
        cfg = self.config.wood_stove
        if not cfg.enabled:
            return {}, []
        series: dict[datetime, float] = {}
        if cfg.binary_entity and cfg.binary_entity in history:
            for point in history[cfg.binary_entity]:
                series[point.moment] = 1.0 if point.value >= 0.5 else 0.0
        elif cfg.temperature_entity and cfg.temperature_entity in history:
            lit = False
            for point in sorted(history[cfg.temperature_entity], key=lambda p: p.moment):
                if lit:
                    lit = point.value >= cfg.lit_below_c
                else:
                    lit = point.value >= cfg.lit_above_c
                series[point.moment] = 1.0 if lit else 0.0
        return series, sorted(series)

    def refresh_wood_stove(self) -> WoodStoveReport:
        """Rebuild stove effects and lighting windows from models + current plan."""
        cfg = self.config.wood_stove
        reading = self.wood_stove.reading
        effects: list[WoodStoveEffect] = []
        for key in cfg.room_keys:
            try:
                room = self.config.room(key)
            except KeyError:
                continue
            model = self.models.get(key, ThermalModel.default())
            eq = None
            if model.k_heat_per_hour > 0.05 and model.k_stove_per_hour > 0.0:
                eq = (model.k_stove_per_hour / model.k_heat_per_hour) * self._nominal_heat_kw(
                    room.heat_share
                )
            effects.append(
                WoodStoveEffect(
                    room_key=key,
                    room_name=room.name,
                    k_stove_per_hour=model.k_stove_per_hour,
                    equivalent_kw=eq,
                    tau_hours=model.tau_hours,
                )
            )

        windows = []
        if self.plan is not None:
            windows = recommend_windows(
                times=self.plan.times,
                price_sek=self.plan.price_sek_per_kwh,
                outdoor_c=self.plan.outdoor_c,
                heat_pump_kw=self.plan.heat_pump_kw,
                step_minutes=self.plan.step_minutes,
            )

        stats = self.store.setting("wood_stove_stats") or {}
        sessions = int(stats.get("sessions", 0)) if isinstance(stats, dict) else 0
        hours = float(stats.get("hours_lit", 0.0)) if isinstance(stats, dict) else 0.0
        self.wood_stove = build_report(
            cfg,
            reading,
            effects,
            windows,
            sessions_observed=sessions,
            hours_lit_observed=hours,
        )
        return self.wood_stove

    def _nominal_heat_kw(self, share: float) -> float:
        total = sum(room.heat_share for room in self.config.rooms) or 1.0
        return self.config.heat_pump.max_thermal_kw * share / total

    def _hot_water_input(
        self, states: dict[str, str], times: list[datetime], prices: list[float]
    ) -> HotWaterInput | None:
        settings = self.config.hot_water
        if not settings.enabled or not settings.top_temperature_entity:
            return None
        current = parse_numeric(states.get(settings.top_temperature_entity))
        if current is None:
            return None

        step_hours = self.config.optimiser.step_minutes / 60.0
        draws = [self.hot_water_profile.expected_kwh(t, step_hours) for t in times]

        return HotWaterInput(
            config=settings,
            initial_temperature=current,
            draw_kwh=draws,
            legionella_step=self._legionella_step(times, prices),
        )

    def _legionella_step(self, times: list[datetime], prices: list[float]) -> int | None:
        """Cheapest quarter on the scheduled legionella night, if it is in range.

        Picking the moment here rather than in the solver keeps the weekly
        pasteurisation a single linear constraint, and picking the cheapest
        one means the hygiene cycle costs as little as it can.
        """
        weekday = self.config.hot_water.legionella_weekday
        if weekday is None:
            return None

        candidates = [
            index
            for index, moment in enumerate(times)
            if moment.weekday() == weekday and 0 <= moment.hour < 6
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda index: prices[index])

    # --- actuation ---------------------------------------------------------
    async def apply(self) -> int:
        """Push the current step of the plan to Home Assistant."""
        if self.plan is None or not self.status.control_enabled:
            return 0

        now = self._now()
        index = self.plan.step_at(now)
        applied = 0

        async with HomeAssistantClient(self.config.home_assistant, self._ha_http) as ha:
            room_writes = 0
            missing_climate: list[str] = []
            try:
                known_states = await ha.states()
            except Exception as exc:  # noqa: BLE001
                known_states = {}
                self._record_error(f"states: {exc}")

            for room_plan in self.plan.rooms:
                room = self.config.room(room_plan.key)
                if not room.climate_entity:
                    continue
                try:
                    if room.climate_entity not in known_states:
                        missing_climate.append(room.climate_entity)
                        self._record_error(
                            f"{room.name}: {room.climate_entity} saknas "
                            "(Entity not found — LK Arc Climate ej tillagd? "
                            "Restart HA efter hemopt-update)"
                        )
                        continue
                    await ha.set_temperature_entity(room.climate_entity, room_plan.setpoint[index])
                    applied += 1
                    room_writes += 1
                except Exception as exc:  # noqa: BLE001 - one room must not stop the rest
                    self._record_error(f"{room.name}: {exc}")

            if missing_climate:
                _LOGGER.error(
                    "Hemopt kunde inte styra rum — climate-entiteter saknas: %s. "
                    "Golvvärme-dashboarden visar då Entity not found. "
                    "Update hemopt → Restart tillägg → Restart Home Assistant. "
                    "Kolla Logs efter «LK Arc Climate check».",
                    ", ".join(missing_climate[:4]),
                )

            # No per-room actuators (common with LK sensors that are read-only):
            # write one house indoor target via H66 / Rego room controller.
            house_entity = self.config.heat_pump.room_setpoint_entity
            if house_entity and room_writes == 0:
                target = self._house_room_setpoint(index)
                if target is not None:
                    try:
                        await ha.set_temperature_entity(house_entity, target)
                        applied += 1
                    except Exception as exc:  # noqa: BLE001
                        self._record_error(f"house room setpoint: {exc}")

            if self.config.hot_water.setpoint_entity and self.plan.hot_water is not None:
                charging = self.plan.hot_water.charge_fraction[index] > 0.05
                target = (
                    self.config.hot_water.max_temperature
                    if charging
                    else self.config.hot_water.min_temperature
                )
                try:
                    await ha.set_temperature_entity(self.config.hot_water.setpoint_entity, target)
                    applied += 1
                except Exception as exc:  # noqa: BLE001
                    self._record_error(f"hot water setpoint: {exc}")

        return applied

    def _house_room_setpoint(self, index: int) -> float | None:
        """Single-circuit indoor target from the plan's priority-1 rooms."""
        if self.plan is None:
            return None
        preferred = [
            room_plan
            for room_plan in self.plan.rooms
            if self.config.room(room_plan.key).priority == 1
        ] or list(self.plan.rooms)
        if not preferred:
            return None
        return sum(room.setpoint[index] for room in preferred) / len(preferred)

    # --- publishing ---------------------------------------------------------
    def _publish(self, plan: Plan, now: datetime) -> None:
        if self._mqtt is None:
            return
        self.status.mqtt_online = self._mqtt.connected
        self._mqtt.publish_state(self.mqtt_payload(plan, now))
        self._mqtt.publish_plan(plan)
        if self.peaks is not None:
            self._mqtt.publish_peak_state(self.peaks)

    def mqtt_payload(self, plan: Plan, now: datetime) -> dict[str, Any]:
        index = plan.step_at(now)
        blocked = plan.block_heating()
        threshold = self.peaks.threshold_kw if self.peaks else 0.0
        in_window = is_peak_window(now, self.config.peak_tariff.window)

        hourly = _hourly_prices(plan)
        cheapest = min(hourly, key=lambda item: item[1]) if hourly else None
        dearest = max(hourly, key=lambda item: item[1]) if hourly else None
        actions = self.model_actions(plan, now)

        payload: dict[str, Any] = {
            "spot_price": round(plan.price_sek_per_kwh[index], 4),
            "total_price": round(plan.price_sek_per_kwh[index], 4),
            "planned_power": plan.heat_pump_kw[index],
            "peak_threshold": round(threshold, 2),
            "peak_average": round(self.peaks.average_kw, 2) if self.peaks else 0.0,
            "peak_cost": round(self.peaks.projected_cost_sek, 1) if self.peaks else 0.0,
            "hour_headroom": round(
                max(self.accumulator.allowed_kw(now, threshold) - plan.heat_pump_kw[index], 0.0)
                if threshold > 0
                else 0.0,
                2,
            ),
            "planned_saving": round(plan.savings_sek, 2),
            "cheapest_hour": cheapest[0].strftime("%H:%M") if cheapest else "",
            "most_expensive_hour": dearest[0].strftime("%H:%M") if dearest else "",
            "plan_status": plan.status,
            "model_action": headline(actions),
            "model_actions": " · ".join(a.title for a in actions[:3]),
            "heating_blocked": _json_bool(blocked[index]),
            "preheating": _json_bool(
                any(room.temperature[index] > room.comfort_max + 0.05 for room in plan.rooms)
            ),
            "peak_guard": _json_bool(self.guard_decision.block),
            "guard_reason": self.guard_decision.reason,
            "advice_saving": round(self.advice.total_annual_saving_sek, 0),
            "advice_top": (
                self.advice.recommendations[0].title
                if self.advice.recommendations
                else "Inga forslag"
            ),
            "in_peak_window": _json_bool(in_window),
            "control_enabled": _json_bool(self.status.control_enabled),
        }

        for room_plan in plan.rooms:
            payload[f"setpoint_{room_plan.key}"] = room_plan.setpoint[index]
            payload[f"priority_{room_plan.key}"] = self.config.room(room_plan.key).priority
            payload[f"inertia_{room_plan.key}"] = round(
                self.models.get(room_plan.key, ThermalModel.default()).tau_hours, 1
            )

        return payload

    def model_actions(self, plan: Plan | None = None, now: datetime | None = None) -> list:
        """Narrative of what the optimiser is doing at `now`."""
        plan = plan if plan is not None else self.plan
        now = now or self._now()
        if plan is None:
            return explain_plan(None, 0, control_enabled=self.status.control_enabled)
        index = plan.step_at(now)
        now_actions = explain_plan(
            plan,
            index,
            control_enabled=self.status.control_enabled,
            guard_blocking=self.guard_decision.block,
            guard_reason=self.guard_decision.reason,
            wood_stove=self.wood_stove,
        )
        return now_actions + explain_upcoming(plan, index)

    def _record_error(self, message: str) -> None:
        _LOGGER.warning(message)
        self.status.errors.append(f"{self._now().strftime('%H:%M')} {message}")
        del self.status.errors[:-20]

    # --- loops -------------------------------------------------------------
    async def run_forever(self) -> None:
        await asyncio.gather(
            self._sample_loop(),
            self._plan_loop(),
            self._train_loop(),
            self._lk_arc_loop(),
        )

    async def _lk_arc_loop(self) -> None:
        """Keep trying until climate.*_thermostat exists (fixes Entity not found)."""
        while True:
            try:
                async with HomeAssistantClient(self.config.home_assistant, self._ha_http) as ha:
                    if await ha.ping():
                        lk = await ha.ensure_lk_arc_climate()
                        if lk.get("ok") and lk.get("action") == "already_present":
                            _LOGGER.info(
                                "LK Arc Climate OK: %s",
                                lk.get("sample_climate") or lk.get("detail"),
                            )
                            await asyncio.sleep(6 * 3600)
                            continue
                        _LOGGER.warning(
                            "LK Arc Climate check: ok=%s action=%s detail=%s",
                            lk.get("ok"),
                            lk.get("action"),
                            lk.get("detail"),
                        )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("LK Arc Climate loop failed")
            await asyncio.sleep(120)

    async def _sample_loop(self) -> None:
        while True:
            try:
                await self.collect()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                _LOGGER.exception("sampling failed")
            await asyncio.sleep(60)

    async def _plan_loop(self) -> None:
        while True:
            try:
                await self.replan()
                await self.apply()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("planning failed")
            await asyncio.sleep(self.config.optimiser.run_interval_minutes * 60)

    async def _train_loop(self) -> None:
        while True:
            try:
                await self.train()
                await self.refresh_advice()
                self.store.housekeeping()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("training failed")
            await asyncio.sleep(6 * 3600)


def _json_bool(value: bool) -> str:
    return "true" if value else "false"


def _to_kw(value: float) -> float:
    return value / 1000.0 if value > 100 else value


def _nearest_value(
    values: dict[datetime, float],
    ordered: list[datetime],
    moment: datetime,
    tolerance_s: int = 3600,
) -> float | None:
    if not ordered:
        return None
    best = min(ordered, key=lambda t: abs((t - moment).total_seconds()))
    if abs((best - moment).total_seconds()) > tolerance_s:
        return None
    return values[best]


def _heat_fraction(
    climate_by_time: dict[datetime, float],
    climate_times: list[datetime],
    moment: datetime,
    indoor: float,
) -> float:
    """Approximate how open the room's loop was.

    With no valve feedback, the thermostat's own setpoint error is the best
    available proxy: a room below its setpoint is calling for heat.
    """
    setpoint = _nearest_value(climate_by_time, climate_times, moment)
    if setpoint is None:
        return 0.0
    error = setpoint - indoor
    return max(0.0, min(1.0, error / 0.5))


def _hourly_prices(plan: Plan) -> list[tuple[datetime, float]]:
    buckets: dict[datetime, list[float]] = {}
    for moment, price in zip(plan.times, plan.price_sek_per_kwh, strict=True):
        hour = moment.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(price)
    return [(hour, sum(values) / len(values)) for hour, values in sorted(buckets.items())]


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "start": plan.start.isoformat(),
        "step_minutes": plan.step_minutes,
        "times": [t.isoformat() for t in plan.times],
        "price": plan.price_sek_per_kwh,
        "outdoor": plan.outdoor_c,
        "heat_pump_kw": plan.heat_pump_kw,
        "total_power_kw": plan.total_power_kw,
        "peak_threshold_kw": plan.peak_threshold_kw,
        "energy_cost_sek": plan.energy_cost_sek,
        "peak_cost_sek": plan.peak_cost_sek,
        "comfort_penalty_sek": plan.comfort_penalty_sek,
        "baseline_energy_cost_sek": plan.baseline_energy_cost_sek,
        "baseline_peak_cost_sek": plan.baseline_peak_cost_sek,
        "status": plan.status,
        "solve_seconds": plan.solve_seconds,
        "notes": plan.notes,
        "rooms": [
            {
                "key": room.key,
                "name": room.name,
                "priority": room.priority,
                "temperature": room.temperature,
                "setpoint": room.setpoint,
                "heat_fraction": room.heat_fraction,
                "comfort_min": room.comfort_min,
                "comfort_max": room.comfort_max,
            }
            for room in plan.rooms
        ],
        "hot_water": (
            {
                "temperature": plan.hot_water.temperature,
                "charge_fraction": plan.hot_water.charge_fraction,
                "expected_draw_kwh": plan.hot_water.expected_draw_kwh,
            }
            if plan.hot_water
            else None
        ),
        "hour_peaks": [
            {
                "hour_start": hour.hour_start.isoformat(),
                "mean_kw": hour.mean_kw,
                "billable": hour.billable,
                "over_threshold_kw": hour.over_threshold_kw,
            }
            for hour in plan.hour_peaks
        ],
    }


def utc_now() -> datetime:
    return datetime.now(UTC)
