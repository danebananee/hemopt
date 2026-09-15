"""Thin add-on entry: bind the port before the optimiser is imported.

`hemopt.engine` pulls in numpy and HiGHS. On a Raspberry Pi that import can
take long enough for Home Assistant's ingress to report 502 / "not ready".
This module stays free of those imports so uvicorn can open :8099 first.
"""

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


def create_app() -> FastAPI:
    holder: dict[str, Any] = {"engine": None, "error": None, "booting": True}

    def get_engine():
        return holder["engine"]

    def require_engine():
        engine = get_engine()
        if engine is None:
            detail = holder.get("error") or "add-on is still starting"
            raise HTTPException(status_code=503, detail=detail)
        return engine

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(_boot(holder))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            engine = holder.get("engine")
            if engine is not None:
                await engine.stop()

    app = FastAPI(
        title="hemopt",
        description="Cost optimisation for heating, hot water and peak power",
        version="0.1.13",
        lifespan=lifespan,
    )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        prefix = request.headers.get("X-Ingress-Path", "").rstrip("/")
        markup = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        markup = markup.replace('<base href="./" />', f'<base href="{prefix}/" />')
        return HTMLResponse(markup)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "ready": get_engine() is not None,
            "error": holder.get("error"),
        }

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        engine = get_engine()
        if engine is None:
            return {
                "starting": True,
                "booting": True,
                "home_assistant_online": False,
                "mqtt_online": False,
                "prices_available": False,
                "forecast_available": False,
                "control_enabled": False,
                "errors": [holder["error"]] if holder.get("error") else [],
                "total_power_entity": None,
            }
        from .api import _status_payload

        payload = _status_payload(engine)
        payload["booting"] = False
        return payload

    @app.get("/api/plan")
    async def plan() -> JSONResponse:
        engine = require_engine()
        from .engine import plan_to_dict

        if engine.plan is None:
            stored = engine.store.latest_plan()
            if stored is None:
                raise HTTPException(status_code=503, detail="no plan computed yet")
            return JSONResponse(stored)
        return JSONResponse(plan_to_dict(engine.plan))

    @app.get("/api/peaks")
    async def peaks() -> dict[str, Any]:
        engine = require_engine()
        from .api import _finite

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
        engine = require_engine()
        from .thermal import ThermalModel

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
                        "r_squared": round(model.r_squared, 3),
                        "samples": model.samples,
                        "fitted": model.fitted,
                    },
                }
            )
        return result

    @app.post("/api/rooms/{key}/priority")
    async def set_priority(key: str, update: PriorityUpdate) -> dict[str, Any]:
        engine = require_engine()
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
        engine = require_engine()
        try:
            room = engine.config.room(key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown room {key}") from exc
        if update.comfort_min >= update.comfort_max:
            raise HTTPException(status_code=400, detail="comfort_min must be below comfort_max")
        room.comfort_min = update.comfort_min
        room.comfort_max = update.comfort_max
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
            raise HTTPException(status_code=503, detail="planning failed, see status for details")
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

    @app.get("/api/advice")
    async def advice() -> dict[str, Any]:
        return require_engine().advice.as_dict()

    @app.post("/api/advice")
    async def recompute_advice() -> dict[str, Any]:
        return (await require_engine().refresh_advice()).as_dict()

    @app.get("/api/meters")
    async def meters() -> dict[str, Any]:
        engine = require_engine()
        from .ha import HomeAssistantClient
        from .meters import suggest_total_power_entities

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
        engine = require_engine()
        from .ha import HomeAssistantClient

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

    from .panel_routes import register_panel_routes

    register_panel_routes(app, require_engine)
    return app


async def _boot(holder: dict[str, Any]) -> None:
    try:
        # Heavy imports happen here — after uvicorn has already bound the port.
        from .api import _adopt_meter_if_missing, _bootstrap_then_run, _build_addon_engine

        engine = await asyncio.to_thread(_build_addon_engine)
        holder["engine"] = engine
        holder["booting"] = False
        await engine.start()
        await _adopt_meter_if_missing(engine)
        await _bootstrap_then_run(engine)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        holder["booting"] = False
        holder["error"] = str(exc)
        _LOGGER.exception("add-on failed to start")
