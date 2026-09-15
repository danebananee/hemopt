"""FastAPI application: JSON API plus the built-in control panel."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Config
from .engine import Engine, plan_to_dict
from .explain import headline
from .ha import HomeAssistantClient
from .meters import pick_default_total_power, suggest_total_power_entities
from .panel_routes import register_panel_routes
from .storage import Store
from .thermal import ThermalModel

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class PriorityUpdate(BaseModel):
    priority: int = Field(ge=1, le=3)


class ComfortUpdate(BaseModel):
    comfort_min: float = Field(ge=5.0, le=30.0)
    comfort_max: float = Field(ge=5.0, le=32.0)


class ControlUpdate(BaseModel):
    enabled: bool


class MeterUpdate(BaseModel):
    entity_id: str | None = None


class LoopMappingApply(BaseModel):
    """Optional confirmation payload for temporary climate_entity swaps."""

    confirm: bool = False
    min_confidence: float = Field(default=0.35, ge=0.0, le=1.0)


def create_addon_app() -> FastAPI:
    """Thin entry used by the add-on — see ``hemopt.addon``."""
    from .addon import create_app as create_thin_app

    return create_thin_app()


def create_app(engine: Engine, run_loops: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await engine.start()
        tasks: list[asyncio.Task] = []
        if run_loops:
            # Sampling history and solving the first plan takes minutes on a
            # Raspberry Pi. Awaiting it here would hold the port closed for
            # that long, and Home Assistant's ingress reports an add-on that
            # refuses connections as "not ready". So bind first, plan after.
            tasks.append(asyncio.create_task(_bootstrap_then_run(engine)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.stop()

    app = FastAPI(
        title="hemopt",
        description="Cost optimisation for heating, hot water and peak power",
        version="0.1.31",
        lifespan=lifespan,
    )

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
        return HTMLResponse(markup)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "ready": True, "error": None}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        payload = _status_payload(engine)
        payload["booting"] = False
        return payload

    @app.get("/api/plan")
    async def plan() -> JSONResponse:
        if engine.plan is None:
            stored = engine.store.latest_plan()
            if stored is None:
                raise HTTPException(status_code=503, detail="no plan computed yet")
            return JSONResponse(stored)
        return JSONResponse(plan_to_dict(engine.plan))

    @app.get("/api/peaks")
    async def peaks() -> dict[str, Any]:
        tariff = engine.config.peak_tariff
        state = engine.peaks
        now = engine._now()  # noqa: SLF001 - same package, intentional
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
                {
                    "day": peak.day.date().isoformat(),
                    "kw": round(peak.kw, 3),
                    "hour": peak.hour,
                }
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

    @app.get("/api/rooms")
    async def rooms() -> list[dict[str, Any]]:
        result = []
        for room in engine.config.rooms:
            model = engine.models.get(room.key, ThermalModel.default())
            result.append(
                {
                    "key": room.key,
                    "name": room.name,
                    "floor": room.floor,
                    "priority": room.priority,
                    "comfort_min": room.comfort_min,
                    "comfort_max": room.comfort_max,
                    "temperature_entity": room.temperature_entity,
                    "climate_entity": room.climate_entity,
                    "model": {
                        "tau_hours": round(model.tau_hours, 1),
                        "k_heat_per_hour": round(model.k_heat_per_hour, 3),
                        "k_gain_per_hour": round(model.k_gain_per_hour, 4),
                        "k_stove_per_hour": round(model.k_stove_per_hour, 3),
                        "r_squared": round(model.r_squared, 3),
                        "samples": model.samples,
                        "fitted": model.fitted,
                    },
                }
            )
        return result

    @app.post("/api/rooms/{key}/priority")
    async def set_priority(key: str, update: PriorityUpdate) -> dict[str, Any]:
        try:
            room = engine.config.room(key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown room {key}") from exc
        room.priority = update.priority
        engine.store.set_setting(f"priority_{key}", update.priority)
        await engine.replan()
        return {"key": key, "priority": room.priority}

    @app.post("/api/rooms/{key}/comfort")
    async def set_comfort(key: str, update: ComfortUpdate) -> dict[str, Any]:
        try:
            room = engine.config.room(key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown room {key}") from exc
        if update.comfort_min >= update.comfort_max:
            raise HTTPException(status_code=400, detail="comfort_min must be below comfort_max")
        room.comfort_min = update.comfort_min
        room.comfort_max = update.comfort_max
        engine.store.set_setting(
            f"comfort_{key}", {"min": room.comfort_min, "max": room.comfort_max}
        )
        await engine.replan()
        return {"key": key, "comfort_min": room.comfort_min, "comfort_max": room.comfort_max}

    @app.post("/api/control")
    async def set_control(update: ControlUpdate) -> dict[str, bool]:
        engine.status.control_enabled = update.enabled
        engine.store.set_setting("control_enabled", update.enabled)
        return {"enabled": update.enabled}

    @app.post("/api/replan")
    async def replan() -> dict[str, Any]:
        plan = await engine.replan()
        if plan is None:
            raise HTTPException(status_code=503, detail="planning failed, see status for details")
        await engine.apply()
        return {"status": plan.status, "solve_seconds": plan.solve_seconds}

    @app.post("/api/train")
    async def train() -> dict[str, Any]:
        await engine.train()
        return {
            "models": {key: round(model.tau_hours, 1) for key, model in engine.models.items()},
            "hot_water_kwh_per_day": round(engine.hot_water_profile.daily_total_kwh(), 2),
        }

    @app.get("/api/loop-mapping")
    async def loop_mapping() -> dict[str, Any]:
        """Temporary diagnostic: cross-correlate thermostat calls vs room rise."""
        report = await engine.analyse_loop_mapping()
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
        return await engine.apply_loop_mapping_swaps(min_confidence=update.min_confidence)

    @app.get("/api/wood-stove")
    async def wood_stove() -> dict[str, Any]:
        engine.refresh_wood_stove()
        return engine.wood_stove.as_dict()

    @app.get("/api/advice")
    async def advice() -> dict[str, Any]:
        return engine.advice.as_dict()

    @app.post("/api/advice")
    async def recompute_advice() -> dict[str, Any]:
        return (await engine.refresh_advice()).as_dict()

    @app.get("/api/meters")
    async def meters() -> dict[str, Any]:
        async with HomeAssistantClient(engine.config.home_assistant, engine._ha_http) as ha:
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
        entity_id = (update.entity_id or "").strip() or None
        if entity_id is not None:
            async with HomeAssistantClient(engine.config.home_assistant, engine._ha_http) as ha:
                if await ha.ping():
                    states = await ha.states()
                    if entity_id not in states:
                        raise HTTPException(
                            status_code=404,
                            detail=f"{entity_id} finns inte i Home Assistant",
                        )
                else:
                    _LOGGER.warning("saving meter %s while Home Assistant is offline", entity_id)
        engine.config.base_load.total_power_entity = entity_id
        engine.config.save_profile()
        _LOGGER.info("total power meter set to %s", entity_id or "(none)")
        return {"current": entity_id}

    register_panel_routes(app, lambda: engine)
    return app


def _build_addon_engine() -> Engine:
    """Construct the engine off the event loop so imports can finish slowly."""
    config = Config.resolve(None)
    if not config.home_assistant.token:
        # Still start: the panel and healthz must answer so ingress stops
        # showing 502. Status will report Home Assistant as offline.
        _LOGGER.error("SUPERVISOR_TOKEN saknas; panelen startar men Home Assistant ar offline")
    return Engine(config, store=Store(config.database_path))


async def _adopt_meter_if_missing(engine: Engine) -> None:
    if engine.config.base_load.total_power_entity:
        return
    try:
        async with HomeAssistantClient(engine.config.home_assistant, engine._ha_http) as ha:
            if not await ha.ping():
                return
            states = await ha.states()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("could not scan for electricity meters")
        return

    chosen = pick_default_total_power(states)
    if chosen is None:
        candidates = suggest_total_power_entities(states)
        if candidates:
            _LOGGER.info(
                "found %d power meter candidates; pick one in the panel",
                len(candidates),
            )
        return

    engine.config.base_load.total_power_entity = chosen
    engine.config.save_profile()
    _LOGGER.info("adopted electricity meter %s", chosen)


async def _bootstrap(engine: Engine) -> None:
    """Get a plan on screen before the periodic loops take over."""
    try:
        async with HomeAssistantClient(engine.config.home_assistant, engine._ha_http) as ha:
            if await ha.ping():
                lk = await ha.ensure_lk_arc_climate()
                _LOGGER.warning(
                    "LK Arc Climate check: ok=%s action=%s detail=%s sample=%s",
                    lk.get("ok"),
                    lk.get("action"),
                    lk.get("detail"),
                    lk.get("sample_climate"),
                )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("LK Arc Climate ensure failed")
    try:
        await engine.collect()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("initial sampling failed")
    try:
        await engine.replan()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("initial planning failed")
    try:
        await engine.refresh_advice()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("initial advice failed")


async def _bootstrap_then_run(engine: Engine) -> None:
    await _bootstrap(engine)
    await engine.run_forever()


def _finite(value: float) -> float | None:
    return None if value == float("inf") else round(value, 3)


def _status_payload(engine: Engine) -> dict[str, Any]:
    status = engine.status
    plan = engine.plan
    now = engine._now()  # noqa: SLF001 - same package, intentional

    payload: dict[str, Any] = {
        "now": now.isoformat(),
        "timezone": engine.config.site.timezone,
        "price_area": engine.config.site.price_area,
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
