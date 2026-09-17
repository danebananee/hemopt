"""Peak settings and usage history for the control panel."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import HTTPException

from hemopt.api import create_app
from hemopt.demo import DemoEngine, demo_config
from hemopt.panel_routes import PeakSettingsUpdate, apply_peak_settings

TZ = ZoneInfo("Europe/Stockholm")


@pytest.fixture
async def client(tmp_path):
    config = demo_config(database_path=str(tmp_path / "test.db"))
    config.optimiser.horizon_hours = 12
    engine = DemoEngine(config)
    app = create_app(engine, run_loops=False)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            await engine.collect()
            await engine.replan()
            yield http, engine


async def test_peak_settings_round_trip(client):
    http, engine = client
    body = (await http.get("/api/settings/peaks")).json()
    assert body["enabled"] is True
    assert body["n_peaks"] == 5

    updated = (
        await http.put(
            "/api/settings/peaks",
            json={
                "enabled": False,
                "n_peaks": 3,
                "price_per_kw_sek": 80,
                "hour_start": 6,
                "hour_end": 22,
                "weekdays_only": False,
                "months": [11, 12, 1, 2],
            },
        )
    ).json()

    assert updated["enabled"] is False
    assert updated["n_peaks"] == 3
    assert updated["price_per_kw_sek"] == 80
    assert engine.config.peak_tariff.enabled is False
    assert engine.config.peak_tariff.window.months == [1, 2, 11, 12]


async def test_history_endpoint_returns_points(client):
    http, engine = client
    now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    for hour in range(24):
        engine.store.record_hourly_power(now - timedelta(hours=hour), 1.5 + hour * 0.01)

    body = (await http.get("/api/history?days=2")).json()
    assert body["days"] == 2
    assert body["resolution"] == "hour"
    assert len(body["points"]) >= 24
    assert body["savings_sek"] is not None


async def test_history_can_aggregate_by_day(client):
    http, engine = client
    now = datetime.now(TZ).replace(minute=0, second=0, microsecond=0)
    for hour in range(48):
        engine.store.record_hourly_power(now - timedelta(hours=hour), 2.0)

    body = (await http.get("/api/history?days=3&resolution=day")).json()
    assert body["resolution"] == "day"
    assert 1 <= len(body["points"]) <= 4
    assert body["sample_count"] >= 48


async def test_prices_endpoint_returns_current_and_series(client):
    http, _engine = client
    body = (await http.get("/api/prices")).json()
    assert body["area"] == "SE3"
    assert body["available"] is True
    assert body["current_total_sek"] is not None or body["points"]
    assert isinstance(body["points"], list)
    if body["points"]:
        assert {"t", "spot", "total"} <= set(body["points"][0])


def test_apply_peak_settings_rejects_empty_months(tmp_path):
    config = demo_config(database_path=str(tmp_path / "x.db"))
    engine = DemoEngine(config)
    with pytest.raises(HTTPException):
        apply_peak_settings(engine, PeakSettingsUpdate(months=[]))
