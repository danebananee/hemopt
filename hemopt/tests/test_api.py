"""End-to-end tests driving the real optimiser through the HTTP API."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from hemopt.api import create_app
from hemopt.demo import DemoEngine, demo_config


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


async def test_healthz(client):
    http, _ = client
    response = await http.get("/healthz")
    assert response.status_code == 200


async def test_index_serves_the_control_panel(client):
    http, _ = client
    response = await http.get("/")
    assert response.status_code == 200
    assert "Kostnadsoptimering" in response.text


async def test_status_reports_a_solved_plan(client):
    http, _ = client
    body = (await http.get("/api/status")).json()

    assert body["plan_status"] == "kOptimal"
    assert body["prices_available"] is True
    assert body["savings_sek"] >= 0


async def test_plan_covers_the_configured_horizon(client):
    http, engine = client
    body = (await http.get("/api/plan")).json()

    assert len(body["times"]) == engine.config.optimiser.steps
    assert len(body["rooms"]) == len(engine.config.rooms)
    assert len(body["heat_pump_kw"]) == len(body["times"])
    assert body["hot_water"] is not None


async def test_plan_respects_comfort_bands(client):
    http, _ = client
    body = (await http.get("/api/plan")).json()

    for room in body["rooms"]:
        floor = room["comfort_min"] - 1.6
        ceiling = room["comfort_max"] + 1.6
        assert min(room["temperature"]) >= floor
        assert max(room["temperature"]) <= ceiling


async def test_peaks_endpoint_exposes_the_billing_model(client):
    http, _ = client
    body = (await http.get("/api/peaks")).json()

    assert body["n_peaks"] == 5
    assert body["marginal_sek_per_kw"] == pytest.approx(body["price_per_kw_sek"] / 5)
    assert body["threshold_kw"] > 0
    assert len(body["counted"]) <= 5


async def test_plan_stays_under_the_peak_threshold(client):
    http, _ = client
    body = (await http.get("/api/plan")).json()

    billable = [hour for hour in body["hour_peaks"] if hour["billable"]]
    assert billable, "the demo window should contain billable hours"
    assert max(hour["over_threshold_kw"] for hour in billable) < 0.05


async def test_priority_change_reshapes_the_plan(client):
    http, engine = client

    await http.post("/api/rooms/entre/priority", json={"priority": 1})
    low = next(r for r in engine.plan.rooms if r.key == "entre")
    low_average = sum(low.temperature) / len(low.temperature)

    await http.post("/api/rooms/entre/priority", json={"priority": 5})
    high = next(r for r in engine.plan.rooms if r.key == "entre")
    high_average = sum(high.temperature) / len(high.temperature)

    assert high_average >= low_average


async def test_priority_is_validated(client):
    http, _ = client
    assert (await http.post("/api/rooms/entre/priority", json={"priority": 9})).status_code == 422
    assert (await http.post("/api/rooms/nope/priority", json={"priority": 3})).status_code == 404


async def test_comfort_band_is_validated(client):
    http, _ = client
    response = await http.post(
        "/api/rooms/entre/comfort", json={"comfort_min": 23.0, "comfort_max": 21.0}
    )
    assert response.status_code == 400


async def test_comfort_change_is_honoured(client):
    http, engine = client
    await http.post("/api/rooms/kontor/comfort", json={"comfort_min": 22.5, "comfort_max": 24.0})

    room = next(r for r in engine.plan.rooms if r.key == "kontor")
    assert min(room.temperature) >= 22.5 - 1.6


async def test_control_toggle_round_trips(client):
    http, engine = client

    await http.post("/api/control", json={"enabled": False})
    assert engine.status.control_enabled is False
    assert (await http.get("/api/status")).json()["control_enabled"] is False

    await http.post("/api/control", json={"enabled": True})
    assert (await http.get("/api/status")).json()["control_enabled"] is True


async def test_rooms_expose_the_learned_inertia(client):
    http, _ = client
    rooms = (await http.get("/api/rooms")).json()

    assert len(rooms) == 9
    for room in rooms:
        assert room["model"]["tau_hours"] > 0
        assert 1 <= room["priority"] <= 5


async def test_replan_endpoint_recomputes(client):
    http, _ = client
    body = (await http.post("/api/replan")).json()

    assert body["status"] == "kOptimal"
    assert body["solve_seconds"] < 20.0


async def test_train_endpoint_returns_model_summary(client):
    http, _ = client
    body = (await http.post("/api/train")).json()

    assert len(body["models"]) == 9
    assert body["hot_water_kwh_per_day"] >= 0
