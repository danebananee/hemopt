"""Panel routes shared by the demo app and the thin add-on entry.

Kept free of heavy imports so the add-on can register them before the
optimiser finishes loading.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class PeakSettingsUpdate(BaseModel):
    enabled: bool | None = None
    n_peaks: int | None = Field(default=None, ge=1, le=10)
    price_per_kw_sek: float | None = Field(default=None, ge=0.0, le=500.0)
    hour_start: int | None = Field(default=None, ge=0, le=23)
    hour_end: int | None = Field(default=None, ge=1, le=24)
    weekdays_only: bool | None = None
    months: list[int] | None = None


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
            raise HTTPException(status_code=400, detail="minst en manad maste valjas")
        tariff.window.months = months
    engine.config.save_profile()
    return peak_settings_payload(engine)


def history_payload(engine, days: int = 14) -> dict[str, Any]:
    days = max(1, min(days, 90))
    now = engine._now()  # noqa: SLF001
    since = now - timedelta(days=days)
    hourly = engine.store.all_hourly_power(since=since)
    points = [
        {"t": moment.isoformat(), "kw": round(kw, 3)} for moment, kw in sorted(hourly.items())
    ]
    plan = engine.plan
    return {
        "days": days,
        "points": points,
        "savings_sek": round(plan.savings_sek, 2) if plan else None,
        "baseline_cost_sek": (
            round(plan.baseline_energy_cost_sek + plan.baseline_peak_cost_sek, 2) if plan else None
        ),
        "optimised_cost_sek": round(plan.total_cost_sek, 2) if plan else None,
    }


async def prices_payload(engine) -> dict[str, Any]:
    """Current spot plus yesterday/today/tomorrow — works without Home Assistant."""
    from .prices import PriceClient
    from .timeutil import is_high_load_energy

    now = engine._now()  # noqa: SLF001
    tz = ZoneInfo(engine.config.site.timezone)
    area = engine.config.site.price_area
    energy = engine.config.energy_price
    window = engine.config.peak_tariff.window

    days = [now.date() - timedelta(days=1), now.date(), now.date() + timedelta(days=1)]
    points: list[dict[str, Any]] = []
    async with PriceClient(area, energy, window, client=engine._http) as client:
        for day in days:
            published = await client.fetch_day(day)
            if not published:
                continue
            for row in published:
                spot = row.spot_sek_per_kwh
                high = is_high_load_energy(row.start, window)
                adder = energy.adder_sek_per_kwh(high)
                if energy.prices_include_vat:
                    total = spot + adder
                else:
                    total = spot * (1.0 + energy.vat_rate) + adder
                points.append(
                    {
                        "t": row.start.isoformat(),
                        "end": row.end.isoformat(),
                        "spot": round(spot, 5),
                        "total": round(total, 5),
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

    # Prefer the live optimiser series when a plan already exists.
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
        "points": points,
        "available": bool(points) or current_total is not None,
    }


def register_panel_routes(app: FastAPI, require_engine: Callable[[], Any]) -> None:
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
    async def usage_history(days: int = 14) -> dict[str, Any]:
        return history_payload(require_engine(), days=days)

    @app.get("/api/prices")
    async def spot_prices() -> dict[str, Any]:
        return await prices_payload(require_engine())
