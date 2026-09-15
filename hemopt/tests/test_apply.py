"""House-level actuation when rooms have sensors but no climate entities."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from hemopt.config import (
    Config,
    HeatPumpConfig,
    HomeAssistantConfig,
    HotWaterConfig,
    OptimiserConfig,
    RoomConfig,
)
from hemopt.engine import Engine
from hemopt.optimizer import HotWaterPlan, Plan, RoomPlan


def _plan(now: datetime, *, room_setpoint: float = 21.0) -> Plan:
    return Plan(
        start=now,
        times=[now],
        step_minutes=15,
        rooms=[
            RoomPlan(
                key="vardagsrum",
                name="Vardagsrum",
                priority=1,
                temperature=[20.5],
                setpoint=[room_setpoint],
                heat_fraction=[0.4],
                comfort_min=20.5,
                comfort_max=22.0,
            ),
            RoomPlan(
                key="kontor",
                name="Kontor",
                priority=2,
                temperature=[19.0],
                setpoint=[18.0],
                heat_fraction=[0.2],
                comfort_min=19.0,
                comfort_max=21.0,
            ),
        ],
        hot_water=HotWaterPlan(
            temperature=[50.0],
            charge_fraction=[0.0],
            expected_draw_kwh=[0.0],
        ),
        heat_pump_kw=[1.2],
        total_power_kw=[2.0],
        price_sek_per_kwh=[1.1],
        outdoor_c=[-2.0],
        hour_peaks=[],
        energy_cost_sek=10.0,
        peak_cost_sek=0.0,
        comfort_penalty_sek=0.0,
    )


async def test_apply_writes_house_setpoint_when_rooms_lack_climate(monkeypatch):
    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/states"):
            return httpx.Response(200, json=[])
        if "/services/" in str(request.url):
            parts = request.url.path.rstrip("/").split("/")
            domain, service = parts[-2], parts[-1]
            seen.append((domain, service, request.read().decode()))
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)

    config = Config(
        home_assistant=HomeAssistantConfig(base_url="http://ha", token="t"),
        heat_pump=HeatPumpConfig(
            room_setpoint_entity="climate.h66_hproom_temp_setpoint",
        ),
        hot_water=HotWaterConfig(
            enabled=True,
            top_temperature_entity="sensor.tank",
            setpoint_entity="number.h66_hpwarm_water_1",
            min_temperature=42.0,
            max_temperature=58.0,
        ),
        rooms=[
            RoomConfig(
                key="vardagsrum",
                name="Vardagsrum",
                priority=1,
                temperature_entity="sensor.vardagsrum",
            ),
            RoomConfig(
                key="kontor",
                name="Kontor",
                priority=2,
                temperature_entity="sensor.kontor",
            ),
        ],
        optimiser=OptimiserConfig(apply_controls=True),
        database_path=":memory:",
    )

    engine = Engine(config)
    engine._ha_http = http  # noqa: SLF001
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(engine, "_now", lambda: now)
    engine.plan = _plan(now, room_setpoint=21.5)
    engine.status.control_enabled = True

    applied = await engine.apply()

    assert applied == 2  # house room setpoint + DHW hold
    assert any("hproom_temp_setpoint" in body and "21.5" in body for _, _, body in seen)
    assert any("hpwarm_water" in body and "42" in body for _, _, body in seen)
    assert all("vardagsrum" not in body for _, _, body in seen)

    await http.aclose()


async def test_apply_rewrites_bare_mac_climate_to_thermostat(monkeypatch, tmp_path):
    """Legacy climate.<mac> in yaml must map onto climate.<mac>_thermostat."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/states"):
            return httpx.Response(
                200,
                json=[
                    {
                        "entity_id": "climate.d5_ba_fd_c1_0c_c1_thermostat",
                        "state": "heat",
                    }
                ],
            )
        if "/services/" in str(request.url):
            seen.append(request.read().decode())
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    profile = tmp_path / "profile.json"
    monkeypatch.setenv("HEMOPT_DATA", str(tmp_path))
    config = Config(
        home_assistant=HomeAssistantConfig(base_url="http://ha", token="t"),
        rooms=[
            RoomConfig(
                key="garderob",
                name="Garderob",
                priority=1,
                temperature_entity="sensor.d5_ba_fd_c1_0c_c1_temperature",
                climate_entity="climate.d5_ba_fd_c1_0c_c1",
            ),
        ],
        optimiser=OptimiserConfig(apply_controls=True),
        database_path=":memory:",
    )
    engine = Engine(config)
    engine._ha_http = http  # noqa: SLF001
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(engine, "_now", lambda: now)
    engine.plan = Plan(
        start=now,
        times=[now],
        step_minutes=15,
        rooms=[
            RoomPlan(
                key="garderob",
                name="Garderob",
                priority=1,
                temperature=[18.0],
                setpoint=[19.0],
                heat_fraction=[0.5],
                comfort_min=18.0,
                comfort_max=21.0,
            )
        ],
        hot_water=None,
        heat_pump_kw=[1.0],
        total_power_kw=[1.5],
        price_sek_per_kwh=[1.0],
        outdoor_c=[0.0],
        hour_peaks=[],
        energy_cost_sek=1.0,
        peak_cost_sek=0.0,
        comfort_penalty_sek=0.0,
    )
    engine.status.control_enabled = True

    applied = await engine.apply()

    assert applied == 1
    assert any("climate.d5_ba_fd_c1_0c_c1_thermostat" in body for body in seen)
    assert config.rooms[0].climate_entity == "climate.d5_ba_fd_c1_0c_c1_thermostat"
    assert profile.exists()
    await http.aclose()


async def test_apply_prefers_per_room_climate_over_house_setpoint(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/states"):
            return httpx.Response(
                200,
                json=[{"entity_id": "climate.vardagsrum", "state": "heat"}],
            )
        if "/services/" in str(request.url):
            seen.append(request.read().decode())
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = Config(
        home_assistant=HomeAssistantConfig(base_url="http://ha", token="t"),
        heat_pump=HeatPumpConfig(
            room_setpoint_entity="climate.h66_hproom_temp_setpoint",
        ),
        rooms=[
            RoomConfig(
                key="vardagsrum",
                name="Vardagsrum",
                priority=1,
                temperature_entity="sensor.vardagsrum",
                climate_entity="climate.vardagsrum",
            ),
        ],
        optimiser=OptimiserConfig(apply_controls=True),
        database_path=":memory:",
    )
    engine = Engine(config)
    engine._ha_http = http  # noqa: SLF001
    now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(engine, "_now", lambda: now)
    engine.plan = Plan(
        start=now,
        times=[now],
        step_minutes=15,
        rooms=[
            RoomPlan(
                key="vardagsrum",
                name="Vardagsrum",
                priority=1,
                temperature=[20.5],
                setpoint=[21.0],
                heat_fraction=[0.5],
                comfort_min=20.5,
                comfort_max=22.0,
            )
        ],
        hot_water=None,
        heat_pump_kw=[1.0],
        total_power_kw=[1.5],
        price_sek_per_kwh=[1.0],
        outdoor_c=[0.0],
        hour_peaks=[],
        energy_cost_sek=1.0,
        peak_cost_sek=0.0,
        comfort_penalty_sek=0.0,
    )
    engine.status.control_enabled = True

    applied = await engine.apply()

    assert applied == 1
    assert any("climate.vardagsrum" in body for body in seen)
    assert all("hproom_temp_setpoint" not in body for body in seen)
    await http.aclose()
