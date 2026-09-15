"""FastAPI application: JSON API plus the built-in control panel."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .engine import Engine, plan_to_dict
from .thermal import ThermalModel

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class PriorityUpdate(BaseModel):
    priority: int = Field(ge=1, le=5)


class ComfortUpdate(BaseModel):
    comfort_min: float = Field(ge=5.0, le=30.0)
    comfort_max: float = Field(ge=5.0, le=32.0)


class ControlUpdate(BaseModel):
    enabled: bool


def create_app(engine: Engine, run_loops: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await engine.start()
        tasks: list[asyncio.Task] = []
        if run_loops:
            await _bootstrap(engine)
            tasks.append(asyncio.create_task(engine.run_forever()))
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
        version="0.1.0",
        lifespan=lifespan,
    )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return _status_payload(engine)

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

    return app


async def _bootstrap(engine: Engine) -> None:
    """Get a plan on screen before the periodic loops take over."""
    try:
        await engine.collect()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("initial sampling failed")
    try:
        await engine.replan()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("initial planning failed")


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
        "fuse_limit_kw": round(engine.config.site.fuse_limit_kw, 2),
        "hot_water_kwh_per_day": round(engine.hot_water_profile.daily_total_kwh(), 2),
    }

    if plan is not None:
        index = plan.step_at(now)
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
        }
    return payload
