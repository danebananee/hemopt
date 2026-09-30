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
from .climate_ids import repair_room_climate_entities, resolve_climate_entity
from .config import Config, clamp_priority
from .explain import explain_plan, explain_upcoming, headline
from .guard import GuardDecision, PeakGuard
from .ha import (
    HomeAssistantClient,
    climate_setpoints,
    is_climate,
    parse_numeric,
    resample_forecast,
)
from .hotwater import TankSample, UsageProfile, build_profile, estimate_draws
from .loop_mapping import (
    LoopMappingReport,
    RoomSeries,
    analyse_loop_mapping,
    apply_climate_swaps,
)
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
from .shadow import (
    LedgerStep,
    TwinOffset,
    call_for_heat,
    cold_degree_hours,
    next_offset,
    reference_step,
    shifted_problem,
    summarise,
)
from .storage import Store
from .thermal import (
    ThermalModel,
    ThermalSample,
    identify,
    nearest_value,
    previous_value,
    slab_alpha,
)
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
        self.guard_decision = GuardDecision(False, "inte utvärderad ännu")
        self.advice = AdviceReport()
        self.wood_stove = WoodStoveReport()
        self._wood_stove_lit = False
        self._ext_state: dict[str, bool] = {}
        # Latest thermostat setpoints; a climate entity's state is only its mode.
        self._setpoints: dict[str, float] = {}
        # Estimated floor output per room, 0..1, tracked between plans.
        self._slab: dict[str, float] = {}
        self._slab_updated: datetime | None = None
        # Whole-house energy in the current planning step, for the ledger.
        self._step_start: datetime | None = None
        self._step_energy_kwh = 0.0
        self._step_covered_h = 0.0
        self._step_last: tuple[datetime, float] | None = None
        self.shadow_offset = TwinOffset.from_dict(self.store.setting("shadow_offset"))

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
            self.models[room.key] = stored or ThermalModel.default(room.resolved_floor_type)
            priority = self.store.setting(f"priority_{room.key}")
            if isinstance(priority, int):
                # Legacy 4–5 meant "hold"; that is priority 1 on the new scale.
                room.priority = 1 if priority >= 4 else clamp_priority(priority)
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
            value = clamp_priority(payload)
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
            rows = await ha.states_full()
        self._setpoints = climate_setpoints(rows)
        return {row["entity_id"]: row["state"] for row in rows}

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
            if is_climate(entity_id):
                value = self._setpoints.get(entity_id)
            else:
                value = parse_numeric(states.get(entity_id))
            if value is not None:
                rows.append((entity_id, now, value))
        self.store.record_samples(rows)
        self.status.last_sample = now
        self._update_wood_stove_reading(states, now)
        self._update_slab_estimates(states, now)

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

        closed = self.accumulator.add_sample(now, total_kw)
        if closed is not None:
            self.store.record_hourly_power(*closed)
        if total_entity:
            self._book_house_energy(now, total_kw)

        self.store.record_hourly_power(
            self.accumulator.hour_start,
            self.accumulator.projected_hour_kw(now, total_kw),
        )

        await self._run_guard(now, states)

    def _update_slab_estimates(self, states: dict[str, str], now: datetime) -> None:
        """Track how much heat each floor is giving off.

        The floor state cannot be measured, but its input can: while hemopt
        steers, the plan says how open each loop is; otherwise the
        thermostat's own setpoint error does. Filtering that through the
        room's learnt floor lag gives the starting point the next plan needs.
        """
        elapsed_h = (
            (now - self._slab_updated).total_seconds() / 3600.0
            if self._slab_updated is not None
            else None
        )
        self._slab_updated = now
        outdoor = (
            parse_numeric(states.get(self.config.heat_pump.outdoor_entity))
            if self.config.heat_pump.outdoor_entity
            else None
        )
        plan_index = self.plan.step_at(now) if self.plan is not None else None
        plan_rooms = {room.key: room for room in self.plan.rooms} if self.plan else {}

        for room in self.config.rooms:
            indoor = parse_numeric(states.get(room.temperature_entity))
            if indoor is None:
                continue
            model = self.models.get(room.key) or ThermalModel.default(room.resolved_floor_type)

            loop: float | None = None
            if self.status.control_enabled and room.key in plan_rooms and plan_index is not None:
                loop = plan_rooms[room.key].heat_fraction[plan_index]
            elif room.climate_entity and room.climate_entity in self._setpoints:
                loop = call_for_heat(self._setpoints[room.climate_entity], indoor)

            current = self._slab.get(room.key)
            if current is None or elapsed_h is None or elapsed_h > 6.0:
                # Nothing to filter from yet: assume the floor is holding the
                # room steady, which is what a thermostat-run house converges to.
                self._slab[room.key] = (
                    model.steady_heat_fraction(indoor, outdoor)
                    if outdoor is not None
                    else (loop if loop is not None else 0.5)
                )
                continue
            if loop is None:
                continue
            alpha = slab_alpha(model.tau_slab_hours, elapsed_h)
            self._slab[room.key] = current + alpha * (loop - current)

    def _book_house_energy(self, now: datetime, total_kw: float) -> None:
        """Integrate whole-house power per planning step for the savings ledger.

        A step is booked when the next one begins, scaled up from the part of
        it that was observed. Steps seen for less than 80 % are left out
        rather than guessed.
        """
        step_minutes = self.config.optimiser.step_minutes
        step_h = step_minutes / 60.0
        step_start = floor_to_step(now, step_minutes)
        last = self._step_last
        self._step_last = (now, total_kw)

        if last is None or not 0 < (now - last[0]).total_seconds() <= step_minutes * 60:
            self._step_start = step_start
            self._step_energy_kwh = 0.0
            self._step_covered_h = 0.0
            return

        previous_moment, previous_kw = last
        if step_start == self._step_start:
            elapsed_h = (now - previous_moment).total_seconds() / 3600.0
            self._step_energy_kwh += 0.5 * (previous_kw + total_kw) * elapsed_h
            self._step_covered_h += elapsed_h
            return

        # The interval crosses a step boundary: split it there.
        span_h = (now - previous_moment).total_seconds() / 3600.0
        before_h = max((step_start - previous_moment).total_seconds() / 3600.0, 0.0)
        boundary_kw = previous_kw + (total_kw - previous_kw) * (before_h / span_h)
        self._step_energy_kwh += 0.5 * (previous_kw + boundary_kw) * before_h
        self._step_covered_h += before_h
        if self._step_start is not None and self._step_covered_h >= 0.8 * step_h:
            self.store.record_house_energy(
                self._step_start, self._step_energy_kwh * step_h / self._step_covered_h
            )
        after_h = span_h - before_h
        self._step_start = step_start
        self._step_energy_kwh = 0.5 * (boundary_kw + total_kw) * after_h
        self._step_covered_h = after_h

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
        if stove_times:
            sessions = 0
            previous = False
            for moment in stove_times:
                lit = stove_on_by_time.get(moment, 0.0) >= 0.5
                if lit and not previous:
                    sessions += 1
                previous = lit
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
                nearest_outdoor = nearest_value(outdoor_series, outdoor_times, point.moment)
                if nearest_outdoor is None:
                    continue
                heat_fraction = _heat_fraction(
                    climate_by_time, climate_times, point.moment, point.value
                )
                stove_on = 0.0
                if room.key in stove_rooms and stove_times:
                    stove_val = nearest_value(stove_on_by_time, stove_times, point.moment)
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

            model = identify(
                samples,
                step_minutes=self.config.optimiser.step_minutes,
                prior=self.models.get(room.key),
                floor_type=room.resolved_floor_type,
            )
            if model.fitted:
                self.models[room.key] = model
                self.store.save_thermal_model(room.key, model)
                _LOGGER.info(
                    "%s: tau %.1f h, floor lag %.1f h, heat %.2f K/h, stove %.2f K/h, "
                    "4 h error %.2f K",
                    room.name,
                    model.tau_hours,
                    model.tau_slab_hours,
                    model.k_heat_per_hour,
                    model.k_stove_per_hour,
                    model.rmse_4h if model.rmse_4h is not None else float("nan"),
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
                    charging=((nearest_value(pump_by_time, pump_times, point.moment) or 0.0) > 0.3),
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

    async def analyse_loop_mapping(self) -> LoopMappingReport:
        """Temporary diagnostic: detect floor-loop ↔ thermostat cross-wiring.

        Passive — reads recorder history only. Not used by the optimiser.
        Removable once the house wiring is verified.
        """
        history_days = self.config.home_assistant.history_days
        start = self._now() - timedelta(days=history_days)
        history = await self.fetch_history(start)
        outdoor_entity = self.config.heat_pump.outdoor_entity
        outdoor_points = history.get(outdoor_entity, []) if outdoor_entity else []
        outdoor = {point.moment: point.value for point in outdoor_points}

        series: list[RoomSeries] = []
        for room in self.config.rooms:
            indoor_points = history.get(room.temperature_entity, [])
            climate_points = history.get(room.climate_entity, []) if room.climate_entity else []
            series.append(
                RoomSeries(
                    key=room.key,
                    name=room.name,
                    climate_entity=room.climate_entity,
                    indoor={point.moment: point.value for point in indoor_points},
                    setpoint={point.moment: point.value for point in climate_points},
                )
            )

        return analyse_loop_mapping(
            series,
            outdoor,
            step_minutes=self.config.optimiser.step_minutes,
            history_days=history_days,
            now=self._now(),
        )

    async def apply_loop_mapping_swaps(self, *, min_confidence: float = 0.35) -> dict[str, Any]:
        """Re-run analysis and apply high-confidence climate_entity swaps.

        Temporary helper for the loop-mapping diagnostic. Writes profile.json
        only — edit hemopt.yaml by hand if that file should stay in sync.
        """
        report = await self.analyse_loop_mapping()
        applied = apply_climate_swaps(
            self.config.rooms, report.suggested_swaps, min_confidence=min_confidence
        )
        if applied:
            self.config.save_profile()
            _LOGGER.info("loop-mapping applied %d climate_entity swap(s)", len(applied))
        payload = report.as_dict()
        payload["applied"] = applied
        return payload

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
        measured_days = _measured_days(history)
        if len(history) < 24:
            self.advice = AdviceReport(
                measured_days=measured_days,
                current_contract=self.config.energy_price.contract,
                notes=[
                    f"För lite mätdata än: {len(history)} timmar sparade "
                    f"({measured_days:.1f} dygn). Rådgivningen behöver minst ett dygn."
                ],
            )
            return self.advice

        load = samples_from_hourly(history)
        spot = await self._historical_spot([sample.start for sample in load])
        if spot is None:
            self.advice = AdviceReport(
                measured_days=measured_days,
                current_contract=self.config.energy_price.contract,
                notes=["Historiska spotpriser kunde inte hämtas."],
            )
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
                    model=self.models.get(room.key)
                    or ThermalModel.default(room.resolved_floor_type),
                    initial_temperature=current,
                    nominal_heat_kw=self._nominal_heat_kw(room.heat_share),
                    initial_slab=self._slab.get(room.key),
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
                "Morgondagens spotpriser saknas, senare delen av planen bygger på dagens profil."
            )

        try:
            await self._book_shadow_step(problem, plan, price_client, prices)
        except InfeasiblePlan as exc:
            _LOGGER.info("shadow step skipped: %s", exc)
        except Exception:  # noqa: BLE001 - the ledger must never stop planning
            _LOGGER.exception("shadow ledger update failed")

        self.plan = plan
        self.status.last_plan = now
        self.store.save_plan(now, plan_to_dict(plan))
        self.refresh_wood_stove()
        self._publish(plan, now)
        return plan

    async def _book_shadow_step(
        self,
        problem: OptimisationInput,
        plan: Plan,
        price_client: PriceClient,
        prices: PriceSeries,
    ) -> None:
        """Advance the two model twins one step and book it in the ledger.

        See `hemopt.shadow` for the method. The real house is always one of
        the twins: the reference twin while hemopt only watches, the optimised
        twin once it steers. The other is the real house plus or minus the
        heat hemopt has shifted so far.
        """
        step_start = problem.start
        if self.store.has_shadow_step(step_start):
            return

        offset = self.shadow_offset
        if self.status.control_enabled:
            # The real house follows the plan; the reference twin is the real
            # house minus the heat hemopt has shifted, run on the setpoints
            # the household had before handing over control.
            optimised = plan
            reference = reference_step(problem, self._manual_setpoints(), offset)
        else:
            reference = reference_step(problem, self._thermostat_setpoints())
            optimised = solve(shifted_problem(problem, offset, +1.0))

        dt = problem.step_hours
        day_points = await price_client.fetch_day(step_start.date()) or []
        day_spot = [point.spot_sek_per_kwh for point in day_points]
        hour = step_start.replace(minute=0, second=0, microsecond=0)
        hour_spot = [
            point.spot_sek_per_kwh
            for point in day_points
            if point.start.replace(minute=0, second=0, microsecond=0) == hour
        ]
        spot = prices.spot[0]
        vat = (
            1.0
            if self.config.energy_price.prices_include_vat
            else (1.0 + self.config.energy_price.vat_rate)
        )

        self.store.record_shadow_step(
            LedgerStep(
                start=step_start,
                spot=spot,
                spot_hour=sum(hour_spot) / len(hour_spot) if hour_spot else spot,
                spot_day=sum(day_spot) / len(day_spot) if day_spot else spot,
                adder=prices.total[0] - spot * vat,
                reference_kwh=reference.electrical_kwh,
                optimised_kwh=optimised.heat_pump_kw[0] * dt,
                house_kwh=None,
                reference_cold_dh=reference.cold_degree_hours,
                optimised_cold_dh=cold_degree_hours(optimised),
                control_enabled=self.status.control_enabled,
            )
        )
        self.shadow_offset = next_offset(optimised, reference)
        self.store.set_setting("shadow_offset", self.shadow_offset.as_dict())

    def _thermostat_setpoints(self) -> dict[str, float | None]:
        """Each room's current thermostat setpoint, remembered for later.

        While hemopt only watches, these are the household's own settings.
        They are saved so the reference twin can keep using them once hemopt
        takes over and starts writing setpoints of its own.
        """
        result: dict[str, float | None] = {}
        for room in self.config.rooms:
            value = self._setpoints.get(room.climate_entity or "")
            result[room.key] = value
        if not self.status.control_enabled and any(v is not None for v in result.values()):
            self.store.set_setting("manual_setpoints", result)
        return result

    def _manual_setpoints(self) -> dict[str, float | None]:
        """The household's own setpoints from before hemopt took control."""
        stored = self.store.setting("manual_setpoints")
        result: dict[str, float | None] = {}
        for room in self.config.rooms:
            value = stored.get(room.key) if isinstance(stored, dict) else None
            if not isinstance(value, (int, float)):
                # Never seen: assume the thermostat sat mid-band.
                value = (room.comfort_min + room.comfort_max) / 2.0
            result[room.key] = float(value)
        return result

    def savings(self, days: int = 30) -> dict[str, Any]:
        """What quarterly pricing plus hemopt would have saved, per day."""
        since = self._now() - timedelta(days=max(1, min(days, 400)))
        steps = self.store.shadow_steps(since, self.tz)
        payload = summarise(steps, self.config, self.tz)
        payload["control_enabled"] = self.status.control_enabled
        return payload

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
            # Heat the plan sends to the stove's rooms is what a fire can
            # replace; beyond that the rooms just get warmer.
            stove_rooms = {effect.room_key for effect in effects}
            displaceable = [0.0] * len(self.plan.times)
            for room_plan in self.plan.rooms:
                if room_plan.key not in stove_rooms:
                    continue
                nominal = self._nominal_heat_kw(self.config.room(room_plan.key).heat_share)
                for index, fraction in enumerate(room_plan.heat_fraction):
                    displaceable[index] += fraction * nominal
            stove_kw = sum(effect.equivalent_kw or 0.0 for effect in effects) or None
            windows = recommend_windows(
                times=self.plan.times,
                price_sek=self.plan.price_sek_per_kwh,
                outdoor_c=self.plan.outdoor_c,
                heat_pump_kw=self.plan.heat_pump_kw,
                step_minutes=self.plan.step_minutes,
                stove_kw=stove_kw,
                displaceable_kw=displaceable,
                cop=[self.config.heat_pump.cop(t) for t in self.plan.outdoor_c],
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
                    entity_id = resolve_climate_entity(
                        room.climate_entity,
                        known_states,
                        temperature_entity=room.temperature_entity,
                    )
                    if entity_id is None:
                        missing_climate.append(room.climate_entity)
                        hint = ""
                        if not room.climate_entity.endswith("_thermostat"):
                            hint = (
                                f" — prova climate.{room.climate_entity.split('.', 1)[-1]}"
                                "_thermostat"
                            )
                        self._record_error(
                            f"{room.name}: {room.climate_entity} saknas "
                            "(Entity not found — LK Arc Climate ej tillagd? "
                            f"Restart HA efter hemopt-update){hint}"
                        )
                        continue
                    if entity_id != room.climate_entity:
                        _LOGGER.warning(
                            "%s: climate_entity %s → %s (auto)",
                            room.name,
                            room.climate_entity,
                            entity_id,
                        )
                        room.climate_entity = entity_id
                        self.config.save_profile()
                    await ha.set_temperature_entity(entity_id, room_plan.setpoint[index])
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
                else "Inga förslag"
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
                            try:
                                states = await ha.states()
                                repaired = repair_room_climate_entities(self.config.rooms, states)
                                if repaired:
                                    self.config.save_profile()
                                    for row in repaired:
                                        _LOGGER.warning(
                                            "climate_entity auto-fix %s: %s → %s",
                                            row["key"],
                                            row["from"],
                                            row["to"],
                                        )
                            except Exception:  # noqa: BLE001
                                _LOGGER.exception("climate_entity auto-fix failed")
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


def _heat_fraction(
    climate_by_time: dict[datetime, float],
    climate_times: list[datetime],
    moment: datetime,
    indoor: float,
) -> float:
    """Approximate how open the room's loop was.

    With no valve feedback, the thermostat's own setpoint error is the best
    available proxy: a room below its setpoint is calling for heat. The
    setpoint in force is the last one set, not the nearest change in time.
    """
    setpoint = previous_value(climate_by_time, climate_times, moment, max_age_s=30 * 86400.0)
    if setpoint is None:
        return 0.0
    return call_for_heat(setpoint, indoor)


def _measured_days(history: dict[datetime, float]) -> float:
    """Calendar span covered by the stored hourly means."""
    if len(history) < 2:
        return len(history) / 24.0
    moments = sorted(history)
    span = moments[-1] - moments[0]
    return (span.total_seconds() + 3600.0) / 86400.0


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
