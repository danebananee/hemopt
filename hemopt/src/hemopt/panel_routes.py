"""HTTP routes for the control panel, shared by the add-on and the CLI server.

Every route lives here once. The add-on registers them before the optimiser
has finished importing, so this module and everything it imports at the top
must stay free of NumPy and HiGHS; the heavy modules are imported inside the
handlers, by which time the engine exists and they are already loaded.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

STATIC_DIR = Path(__file__).parent / "static"
VERSION = "0.3.0"


class PriorityUpdate(BaseModel):
    priority: int = Field(ge=1, le=3)


class ComfortUpdate(BaseModel):
    comfort_min: float = Field(ge=5.0, le=30.0)
    comfort_max: float = Field(ge=5.0, le=32.0)


class ControlUpdate(BaseModel):
    enabled: bool


class StoveUpdate(BaseModel):
    lit: bool


class MeterUpdate(BaseModel):
    entity_id: str | None = None


class LoopMappingApply(BaseModel):
    """Optional confirmation payload for temporary climate_entity swaps."""

    confirm: bool = False
    min_confidence: float = Field(default=0.35, ge=0.0, le=1.0)


class PeakSettingsUpdate(BaseModel):
    enabled: bool | None = None
    n_peaks: int | None = Field(default=None, ge=1, le=10)
    price_per_kw_sek: float | None = Field(default=None, ge=0.0, le=500.0)
    hour_start: int | None = Field(default=None, ge=0, le=23)
    hour_end: int | None = Field(default=None, ge=1, le=24)
    weekdays_only: bool | None = None
    months: list[int] | None = None


def _asset_hash(name: str) -> str:
    """Short content hash of a static file, for cache busting."""
    import hashlib

    try:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return VERSION


def _finite(value: float) -> float | None:
    return None if value == float("inf") else round(value, 3)


def peak_settings_payload(engine) -> dict[str, Any]:
    tariff = engine.config.peak_tariff
    return {
        "enabled": tariff.enabled,
        "n_peaks": tariff.n_peaks,
        "price_per_kw_sek": tariff.price_per_kw_sek,
        "marginal_sek_per_kw": tariff.marginal_price_per_kw,
        "hour_start": tariff.window.hour_start,
        "hour_end": tariff.window.hour_end,
        "weekdays_only": tariff.window.weekdays_only,
        "months": list(tariff.window.months),
        "prices_include_vat": tariff.prices_include_vat,
        "source": "configuration",
    }


def apply_peak_settings(engine, update: PeakSettingsUpdate) -> dict[str, Any]:
    """Apply peak rules from an API call (demo / tests).

    On the add-on the live source of truth is the Configuration tab (and
    ``hemopt.yaml`` for months). Dashboard edits are no longer offered.
    """
    tariff = engine.config.peak_tariff
    if update.enabled is not None:
        tariff.enabled = update.enabled
    if update.n_peaks is not None:
        tariff.n_peaks = update.n_peaks
    if update.price_per_kw_sek is not None:
        tariff.price_per_kw_sek = update.price_per_kw_sek
    if update.hour_start is not None:
        tariff.window.hour_start = update.hour_start
    if update.hour_end is not None:
        tariff.window.hour_end = update.hour_end
    if update.weekdays_only is not None:
        tariff.window.weekdays_only = update.weekdays_only
    if update.months is not None:
        months = sorted({month for month in update.months if 1 <= month <= 12})
        if not months:
            raise HTTPException(status_code=400, detail="minst en månad måste väljas")
        tariff.window.months = months
    engine.config.save_profile()
    return peak_settings_payload(engine)


def status_payload(engine) -> dict[str, Any]:
    from .explain import headline

    status = engine.status
    plan = engine.plan
    now = engine._now()  # noqa: SLF001 - same package, intentional

    payload: dict[str, Any] = {
        "now": now.isoformat(),
        "version": VERSION,
        "booting": False,
        "timezone": engine.config.site.timezone,
        "price_area": engine.config.site.price_area,
        "contract": engine.config.energy_price.contract,
        "home_assistant_online": status.home_assistant_online,
        "mqtt_online": status.mqtt_online,
        "prices_available": status.prices_available,
        "forecast_available": status.forecast_available,
        "control_enabled": status.control_enabled,
        "last_sample": status.last_sample.isoformat() if status.last_sample else None,
        "last_plan": status.last_plan.isoformat() if status.last_plan else None,
        "last_training": status.last_training.isoformat() if status.last_training else None,
        "errors": list(status.errors[-5:]),
        # The panel is reachable before the first plan exists, so the UI needs
        # to tell "still warming up" apart from "planning failed".
        "starting": plan is None and status.last_plan is None,
        "fuse_limit_kw": round(engine.config.site.fuse_limit_kw, 2),
        "hot_water_kwh_per_day": round(engine.hot_water_profile.daily_total_kwh(), 2),
        "total_power_entity": engine.config.base_load.total_power_entity,
        "rooms_configured": len(engine.config.rooms),
        "ha_base_url": engine.config.home_assistant.base_url,
        "ha_token_present": bool(engine.config.home_assistant.token),
        "ha_diagnosis": status.ha_diagnosis,
        "mqtt_configured": bool(engine.config.mqtt.enabled and engine.config.mqtt.host),
        "weather_entity": engine.config.site.weather_entity,
        "peak_enabled": engine.config.peak_tariff.enabled,
        "hot_water_enabled": bool(
            engine.config.hot_water.enabled and engine.config.hot_water.top_temperature_entity
        ),
        "hot_water_setpoint_entity": engine.config.hot_water.setpoint_entity,
        "heat_pump_power_entity": engine.config.heat_pump.power_entity,
        "heat_pump_outdoor_entity": engine.config.heat_pump.outdoor_entity,
        "room_setpoint_entity": engine.config.heat_pump.room_setpoint_entity,
        "rooms_with_climate": sum(1 for room in engine.config.rooms if room.climate_entity),
        "ext_enabled": engine.config.ext_control.enabled,
        "guard_blocking": engine.guard_decision.block,
        "guard_reason": engine.guard_decision.reason,
        "wood_stove_enabled": engine.config.wood_stove.enabled,
        "power_backfill": engine.store.setting("power_backfill"),
        "location_known": engine._location is not None,  # noqa: SLF001
    }

    # Spot is independent of Home Assistant — surface it even before a plan.
    if engine.prices is not None and engine.prices.times:
        index = 0
        for i, moment in enumerate(engine.prices.times):
            if moment <= now:
                index = i
            else:
                break
        payload["current_spot_sek"] = round(engine.prices.spot[index], 4)
        payload["current_price_sek"] = round(engine.prices.total[index], 4)

    if plan is not None:
        index = plan.step_at(now)
        actions = engine.model_actions(plan, now)
        payload |= {
            "current_price_sek": round(plan.price_sek_per_kwh[index], 4),
            "planned_power_kw": plan.heat_pump_kw[index],
            "energy_cost_sek": plan.energy_cost_sek,
            "peak_cost_sek": plan.peak_cost_sek,
            "total_cost_sek": round(plan.total_cost_sek, 2),
            "savings_sek": round(plan.savings_sek, 2),
            "baseline_cost_sek": round(
                plan.baseline_energy_cost_sek + plan.baseline_peak_cost_sek, 2
            ),
            "comfort_penalty_sek": plan.comfort_penalty_sek,
            "solve_seconds": plan.solve_seconds,
            "plan_status": plan.status,
            "horizon_hours": round(len(plan.times) * plan.step_minutes / 60.0, 1),
            "notes": plan.notes,
            "model_actions": [a.as_dict() for a in actions],
            "model_action": headline(actions),
        }
    return payload


def rooms_payload(engine) -> list[dict[str, Any]]:
    from .thermal import ThermalModel

    plan = engine.plan
    now = engine._now()  # noqa: SLF001
    index = plan.step_at(now) if plan is not None else None
    planned = {room.key: room for room in plan.rooms} if plan is not None else {}

    def latest(entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        sample = engine.store.latest_sample(entity_id)
        return sample[1] if sample is not None else None

    result = []
    for room in engine.config.rooms:
        model = engine.models.get(room.key) or ThermalModel.default(room.resolved_floor_type)
        room_plan = planned.get(room.key)
        setpoint = None
        heat = None
        if room_plan is not None and index is not None:
            setpoint = room_plan.setpoint[index]
            heat = room_plan.heat_fraction[index]
        thermostat = engine._setpoints.get(room.climate_entity or "")  # noqa: SLF001
        result.append(
            {
                "key": room.key,
                "name": room.name,
                "floor": room.floor,
                "floor_type": room.resolved_floor_type,
                "priority": room.priority,
                "comfort_min": room.comfort_min,
                "comfort_max": room.comfort_max,
                "temperature": _round(latest(room.temperature_entity), 2),
                "humidity": _round(latest(room.humidity_entity), 0),
                "thermostat_setpoint": _round(thermostat, 1),
                "planned_setpoint": setpoint,
                "planned_heat": heat,
                "temperature_entity": room.temperature_entity,
                "climate_entity": room.climate_entity,
                "model": {
                    "tau_hours": round(model.tau_hours, 1),
                    "tau_slab_hours": round(model.tau_slab_hours, 2),
                    "k_heat_per_hour": round(model.k_heat_per_hour, 3),
                    "k_gain_per_hour": round(model.k_gain_per_hour, 4),
                    "k_stove_per_hour": round(model.k_stove_per_hour, 3),
                    "r_squared": round(model.r_squared, 3),
                    "rmse_4h": model.rmse_4h,
                    "k_sun_per_hour": round(model.k_sun_per_hour, 3),
                    "k_wind_per_hour": round(model.k_wind_per_hour, 5),
                    "history_days": model.history_days,
                    "samples": model.samples,
                    "fitted": model.fitted,
                },
            }
        )
    return result


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(value, digits) if digits else float(round(value))


def peaks_payload(engine) -> dict[str, Any]:
    tariff = engine.config.peak_tariff
    state = engine.peaks
    now = engine._now()  # noqa: SLF001
    return {
        "enabled": tariff.enabled,
        "n_peaks": tariff.n_peaks,
        "price_per_kw_sek": tariff.effective_price_per_kw,
        "marginal_sek_per_kw": tariff.marginal_price_per_kw,
        "window": tariff.window.model_dump(),
        "threshold_kw": round(state.threshold_kw, 3) if state else 0.0,
        "average_kw": round(state.average_kw, 3) if state else 0.0,
        "projected_cost_sek": round(state.projected_cost_sek, 2) if state else 0.0,
        "counted": [
            {"day": peak.day.date().isoformat(), "kw": round(peak.kw, 3), "hour": peak.hour}
            for peak in (state.counted if state else [])
        ],
        "current_hour": {
            "hour_start": engine.accumulator.hour_start.isoformat(),
            "energy_kwh": round(engine.accumulator.energy_kwh, 3),
            "minutes_remaining": round(engine.accumulator.minutes_remaining(now), 1),
            "allowed_kw": _finite(
                engine.accumulator.allowed_kw(now, state.threshold_kw if state else 0.0)
            ),
        },
        "history": engine.store.month_results(limit=12),
    }


def history_payload(engine, days: int = 14, resolution: str = "hour") -> dict[str, Any]:
    days = max(1, min(int(days), 366))
    resolution = (resolution or "hour").lower()
    if resolution not in {"hour", "day", "week", "month", "quarter"}:
        resolution = "hour"
    now = engine._now()  # noqa: SLF001
    since = now - timedelta(days=days)
    hourly = engine.store.all_hourly_power(since=since)
    raw = sorted(hourly.items())
    points = _aggregate_power_points(raw, resolution)

    # Each stored value is the mean power over one clock hour, so kWh == kW·1 h.
    total_kwh = sum(kw for _, kw in raw)
    hours = len(raw)
    mean_kw = total_kwh / hours if hours else 0.0
    peak_hour = max(raw, key=lambda item: item[1]) if raw else None

    # Momentary readings from the house meter, so a household can see the
    # difference between a short burst and the hourly mean that is billed.
    live: list[dict[str, Any]] = []
    meter = engine.config.base_load.total_power_entity
    if meter and resolution == "hour" and days <= 7:
        for moment, value in engine.store.samples(meter, since):
            kw = value / 1000.0 if value > 100 else value
            live.append({"t": moment.isoformat(), "kw": round(kw, 3)})

    state = engine.peaks
    tariff = engine.config.peak_tariff
    counted = [
        {"day": peak.day.date().isoformat(), "hour": peak.hour, "kw": round(peak.kw, 3)}
        for peak in (state.counted if state else [])
    ]

    plan = engine.plan
    return {
        "days": days,
        "resolution": resolution,
        "points": points,
        "live_points": live,
        "sample_count": hours,
        "hours_measured": hours,
        "measured_days": round(hours / 24.0, 2),
        "total_kwh": round(total_kwh, 2),
        "mean_kw": round(mean_kw, 3),
        "kwh_per_day": round(total_kwh / (hours / 24.0), 2) if hours >= 1 else None,
        "peak_hour_kw": round(peak_hour[1], 3) if peak_hour else None,
        "peak_hour_at": peak_hour[0].isoformat() if peak_hour else None,
        "peak_enabled": tariff.enabled,
        "peak_n": tariff.n_peaks,
        "peak_threshold_kw": round(state.threshold_kw, 3) if state else None,
        "peak_average_kw": round(state.average_kw, 3) if state else None,
        "peak_counted": counted,
        "plan_hours": (
            round(len(plan.times) * plan.step_minutes / 60.0, 1) if plan and plan.times else None
        ),
        "savings_sek": round(plan.savings_sek, 2) if plan else None,
        "baseline_cost_sek": (
            round(plan.baseline_energy_cost_sek + plan.baseline_peak_cost_sek, 2) if plan else None
        ),
        "optimised_cost_sek": round(plan.total_cost_sek, 2) if plan else None,
    }


def _aggregate_power_points(
    raw: list[tuple[datetime, float]], resolution: str
) -> list[dict[str, Any]]:
    """Bucket hourly means into coarser periods.

    `kw` is the mean power in the bucket, `kwh` the energy it represents. One
    stored sample is one clock hour, so energy is simply the sum of the means.
    """
    if resolution == "hour" or not raw:
        return [
            {"t": moment.isoformat(), "kw": round(kw, 3), "kwh": round(kw, 3)} for moment, kw in raw
        ]

    buckets: dict[datetime, list[float]] = {}
    for moment, kw in raw:
        if resolution == "day":
            key = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        elif resolution == "week":
            day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            key = day - timedelta(days=day.weekday())
        elif resolution == "month":
            key = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:  # quarter
            month = ((moment.month - 1) // 3) * 3 + 1
            key = moment.replace(month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
        buckets.setdefault(key, []).append(kw)

    return [
        {
            "t": key.isoformat(),
            "kw": round(sum(values) / len(values), 3),
            "kwh": round(sum(values), 2),
            "hours": len(values),
        }
        for key, values in sorted(buckets.items())
    ]


async def prices_payload(engine, days_back: int = 1, days_forward: int = 1) -> dict[str, Any]:
    """Spot plus surrounding days — works without Home Assistant."""
    from .prices import PriceClient
    from .timeutil import is_high_load_energy

    days_back = max(0, min(int(days_back), 31))
    days_forward = max(0, min(int(days_forward), 2))

    now = engine._now()  # noqa: SLF001
    tz = ZoneInfo(engine.config.site.timezone)
    area = engine.config.site.price_area
    energy = engine.config.energy_price
    window = engine.config.peak_tariff.window

    days = (
        [now.date() - timedelta(days=offset) for offset in range(days_back, 0, -1)]
        + [now.date()]
        + [now.date() + timedelta(days=offset) for offset in range(1, days_forward + 1)]
    )
    points: list[dict[str, Any]] = []
    async with PriceClient(area, energy, window, client=engine._http) as client:  # noqa: SLF001
        for day in days:
            published = await client.fetch_day(day)
            if not published:
                continue
            for row in published:
                spot = row.spot_sek_per_kwh
                adder = energy.adder_sek_per_kwh(is_high_load_energy(row.start, window))
                vat = 1.0 if energy.prices_include_vat else 1.0 + energy.vat_rate
                points.append(
                    {
                        "t": row.start.isoformat(),
                        "end": row.end.isoformat(),
                        "spot": round(spot, 5),
                        "total": round(spot * vat + adder, 5),
                        "day": day.isoformat(),
                    }
                )

    current_spot = None
    current_total = None
    for point in points:
        start = datetime.fromisoformat(point["t"])
        end = datetime.fromisoformat(point["end"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)
        if start <= now < end:
            current_spot = point["spot"]
            current_total = point["total"]
            break

    if current_total is None and engine.prices is not None and engine.prices.times:
        series = engine.prices
        index = 0
        for i, moment in enumerate(series.times):
            if moment <= now:
                index = i
            else:
                break
        current_spot = round(series.spot[index], 5)
        current_total = round(series.total[index], 5)

    return {
        "area": area,
        "now": now.isoformat(),
        "current_spot_sek": current_spot,
        "current_total_sek": current_total,
        "days_back": days_back,
        "days_forward": days_forward,
        "points": points,
        "available": bool(points) or current_total is not None,
    }


def register_panel_routes(
    app: FastAPI,
    get_engine: Callable[[], Any],
    boot_error: Callable[[], str | None] = lambda: None,
) -> None:
    """Mount the panel and every API route on `app`.

    `get_engine` returns None while the add-on is still starting; routes that
    need the engine answer 503 until then, while the page, healthz and status
    answer right away.
    """

    def require_engine():
        engine = get_engine()
        if engine is None:
            detail = boot_error() or "hemopt startar fortfarande"
            raise HTTPException(status_code=503, detail=detail)
        return engine

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        # Home Assistant's ingress serves the panel under a per-session prefix
        # and passes it in X-Ingress-Path. Without a matching <base>, the
        # page's own relative requests for assets and the API would resolve
        # against Home Assistant itself instead of the add-on.
        prefix = request.headers.get("X-Ingress-Path", "").rstrip("/")
        markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        markup = markup.replace('<base href="./" />', f'<base href="{prefix}/" />')
        # Browsers and the ingress proxy keep serving a cached stylesheet and
        # script after an update, which pairs new markup with old code and
        # leaves the panel dead. A content hash in each asset URL forces a
        # fresh copy whenever the file changes, and the page itself is never
        # cached.
        for asset in ("styles.css", "app.js"):
            markup = markup.replace(f"static/{asset}", f"static/{asset}?v={_asset_hash(asset)}")
        return HTMLResponse(markup, headers={"Cache-Control": "no-cache, no-store"})

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "ready": get_engine() is not None, "error": boot_error()}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        engine = get_engine()
        if engine is None:
            error = boot_error()
            return {
                "starting": True,
                "booting": True,
                "version": VERSION,
                "home_assistant_online": False,
                "mqtt_online": False,
                "prices_available": False,
                "forecast_available": False,
                "control_enabled": False,
                "errors": [error] if error else [],
                "total_power_entity": None,
            }
        return status_payload(engine)

    @app.get("/api/plan")
    async def plan() -> JSONResponse:
        from .engine import plan_to_dict

        engine = require_engine()
        if engine.plan is None:
            stored = engine.store.latest_plan()
            if stored is None:
                raise HTTPException(status_code=503, detail="ingen plan beräknad ännu")
            return JSONResponse(stored)
        return JSONResponse(plan_to_dict(engine.plan))

    @app.get("/api/peaks")
    async def peaks() -> dict[str, Any]:
        return peaks_payload(require_engine())

    @app.get("/api/rooms")
    async def rooms() -> list[dict[str, Any]]:
        return rooms_payload(require_engine())

    @app.post("/api/rooms/{key}/priority")
    async def set_priority(key: str, update: PriorityUpdate) -> dict[str, Any]:
        engine = require_engine()
        try:
            room = engine.config.room(key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"okänt rum {key}") from exc
        room.priority = update.priority
        engine.store.set_setting(f"priority_{key}", update.priority)
        await engine.replan()
        return {"key": key, "priority": room.priority}

    @app.post("/api/rooms/{key}/comfort")
    async def set_comfort(key: str, update: ComfortUpdate) -> dict[str, Any]:
        engine = require_engine()
        try:
            room = engine.config.room(key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"okänt rum {key}") from exc
        if update.comfort_min >= update.comfort_max:
            raise HTTPException(status_code=400, detail="lägsta måste vara under högsta")
        room.comfort_min = update.comfort_min
        room.comfort_max = update.comfort_max
        engine.store.set_setting(
            f"comfort_{key}", {"min": room.comfort_min, "max": room.comfort_max}
        )
        await engine.replan()
        return {"key": key, "comfort_min": room.comfort_min, "comfort_max": room.comfort_max}

    @app.post("/api/control")
    async def set_control(update: ControlUpdate) -> dict[str, bool]:
        engine = require_engine()
        engine.status.control_enabled = update.enabled
        engine.store.set_setting("control_enabled", update.enabled)
        return {"enabled": update.enabled}

    @app.post("/api/replan")
    async def replan() -> dict[str, Any]:
        engine = require_engine()
        plan = await engine.replan()
        if plan is None:
            raise HTTPException(status_code=503, detail="planeringen misslyckades, se status")
        await engine.apply()
        return {"status": plan.status, "solve_seconds": plan.solve_seconds}

    @app.post("/api/train")
    async def train() -> dict[str, Any]:
        engine = require_engine()
        await engine.train()
        return {
            "models": {key: round(model.tau_hours, 1) for key, model in engine.models.items()},
            "hot_water_kwh_per_day": round(engine.hot_water_profile.daily_total_kwh(), 2),
        }

    @app.get("/api/savings")
    async def savings(days: int = 30) -> dict[str, Any]:
        return require_engine().savings(days)

    @app.get("/api/loop-mapping")
    async def loop_mapping() -> dict[str, Any]:
        """Temporary diagnostic: cross-correlate thermostat calls vs room rise."""
        report = await require_engine().analyse_loop_mapping()
        return report.as_dict()

    @app.post("/api/loop-mapping/apply")
    async def loop_mapping_apply(update: LoopMappingApply) -> dict[str, Any]:
        """Apply high-confidence climate_entity swaps from a fresh analysis.

        Temporary — confirm=true required. Does not touch physical valves.
        """
        if not update.confirm:
            raise HTTPException(
                status_code=400,
                detail='skicka {"confirm": true} för att byta climate_entity',
            )
        return await require_engine().apply_loop_mapping_swaps(min_confidence=update.min_confidence)

    @app.get("/api/wood-stove")
    async def wood_stove() -> dict[str, Any]:
        engine = require_engine()
        engine.refresh_wood_stove()
        return engine.wood_stove.as_dict()

    @app.post("/api/wood-stove/lit")
    async def mark_wood_stove(update: StoveUpdate) -> dict[str, Any]:
        """The household marks a fire as lit or out; the models learn from it."""
        return require_engine().mark_stove(update.lit).as_dict()

    @app.get("/api/advice")
    async def advice() -> dict[str, Any]:
        return require_engine().advice.as_dict()

    @app.post("/api/advice")
    async def recompute_advice() -> dict[str, Any]:
        return (await require_engine().refresh_advice()).as_dict()

    @app.get("/api/meters")
    async def meters() -> dict[str, Any]:
        from .ha import HomeAssistantClient
        from .meters import suggest_total_power_entities

        engine = require_engine()
        async with HomeAssistantClient(engine.config.home_assistant, engine._ha_http) as ha:  # noqa: SLF001
            if not await ha.ping():
                return {
                    "current": engine.config.base_load.total_power_entity,
                    "candidates": [],
                    "online": False,
                }
            states = await ha.states()
        return {
            "current": engine.config.base_load.total_power_entity,
            "candidates": suggest_total_power_entities(states),
            "online": True,
        }

    @app.put("/api/meters/total")
    async def set_total_meter(update: MeterUpdate) -> dict[str, Any]:
        from .ha import HomeAssistantClient

        engine = require_engine()
        entity_id = (update.entity_id or "").strip() or None
        if entity_id is not None:
            async with HomeAssistantClient(engine.config.home_assistant, engine._ha_http) as ha:  # noqa: SLF001
                if await ha.ping():
                    states = await ha.states()
                    if entity_id not in states:
                        raise HTTPException(
                            status_code=404,
                            detail=f"{entity_id} finns inte i Home Assistant",
                        )
        engine.config.base_load.total_power_entity = entity_id
        engine.config.save_profile()
        return {"current": entity_id}

    @app.get("/api/settings/peaks")
    async def get_peak_settings() -> dict[str, Any]:
        return peak_settings_payload(require_engine())

    @app.put("/api/settings/peaks")
    async def put_peak_settings(update: PeakSettingsUpdate) -> dict[str, Any]:
        engine = require_engine()
        payload = apply_peak_settings(engine, update)
        await engine.replan()
        return payload

    @app.get("/api/history")
    async def usage_history(days: int = 14, resolution: str = "hour") -> dict[str, Any]:
        return history_payload(require_engine(), days=days, resolution=resolution)

    @app.get("/api/prices")
    async def spot_prices(days_back: int = 1, days_forward: int = 1) -> dict[str, Any]:
        return await prices_payload(
            require_engine(), days_back=days_back, days_forward=days_forward
        )
