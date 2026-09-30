"""The client must address the configured host even with a borrowed transport."""

from __future__ import annotations

import httpx

from hemopt.config import HomeAssistantConfig
from hemopt.ha import HomeAssistantClient


def _recording_client() -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A shared client like the engine's: no base URL and no auth headers."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # Absolute supervisor URLs parse as host=supervisor, path=/core/api/…
        if request.url.path.endswith("/api/config"):
            return httpx.Response(200, json={"location_name": "Home", "version": "2024.1"})
        return httpx.Response(200, json=[{"entity_id": "sensor.inne", "state": "21.4"}])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def test_shared_client_still_reaches_the_configured_home_assistant():
    http, seen = _recording_client()
    client = HomeAssistantClient(
        HomeAssistantConfig(base_url="http://supervisor/core/", token="supervisor-token"),
        client=http,
    )

    async with client:
        assert await client.ping() is True
        assert await client.states() == {"sensor.inne": "21.4"}

    assert [str(request.url) for request in seen] == [
        "http://supervisor/core/api/config",
        "http://supervisor/core/api/states",
    ]
    assert {request.headers["Authorization"] for request in seen} == {"Bearer supervisor-token"}


async def test_service_calls_carry_the_token_on_a_shared_client():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = HomeAssistantClient(
        HomeAssistantConfig(base_url="http://supervisor/core", token="supervisor-token"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with client:
        await client.set_climate_temperature("climate.vardagsrum", 21.5)

    assert str(seen[0].url) == "http://supervisor/core/api/services/climate/set_temperature"
    assert seen[0].headers["Authorization"] == "Bearer supervisor-token"


async def test_set_temperature_entity_routes_climate_and_number():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = HomeAssistantClient(
        HomeAssistantConfig(base_url="http://ha", token="t"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async with client:
        await client.set_temperature_entity("climate.h66_hproom_temp_setpoint", 21.0)
        await client.set_temperature_entity("number.h66_hpwarm_water_1", 52.0)

    assert seen[0].url.path.endswith("/climate/set_temperature")
    assert seen[1].url.path.endswith("/number/set_value")


async def test_ensure_lk_arc_climate_starts_flow_when_missing():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/api/states"):
            return httpx.Response(
                200,
                json=[
                    {"entity_id": "sensor.e0_ec_2c_c8_5e_2c_temperature", "state": "21.4"},
                    {"entity_id": "climate.h66_hproom_temp_setpoint", "state": "21"},
                ],
            )
        if request.url.path.endswith("/api/config/config_entries/flow"):
            return httpx.Response(
                200,
                json={"type": "create_entry", "title": "LK Arc Climate", "flow_id": "x"},
            )
        if "/services/persistent_notification/" in request.url.path:
            return httpx.Response(200, json={})
        return httpx.Response(404)

    client = HomeAssistantClient(
        HomeAssistantConfig(base_url="http://ha", token="t"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    async with client:
        result = await client.ensure_lk_arc_climate()

    assert result["ok"] is True
    assert result["action"] == "create_entry"
    assert any(r.url.path.endswith("/api/config/config_entries/flow") for r in seen)


async def test_ensure_lk_arc_climate_skips_when_present():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/states"):
            return httpx.Response(
                200,
                json=[
                    {
                        "entity_id": "climate.e0_ec_2c_c8_5e_2c_thermostat",
                        "state": "heat",
                    }
                ],
            )
        return httpx.Response(500, text="should not call flow")

    client = HomeAssistantClient(
        HomeAssistantConfig(base_url="http://ha", token="t"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    async with client:
        result = await client.ensure_lk_arc_climate()

    assert result["ok"] is True
    assert result["action"] == "already_present"
    assert result["sample_climate"] == "climate.e0_ec_2c_c8_5e_2c_thermostat"


async def test_climate_history_reports_the_setpoint_not_the_mode():
    """A thermostat's state is 'heat'; the setpoint lives in its attributes."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "climate.salong" in request.url.params.get("filter_entity_id", ""):
            return httpx.Response(
                200,
                json=[
                    [
                        {
                            "entity_id": "climate.salong",
                            "state": "heat",
                            "attributes": {"temperature": 21.0},
                            "last_changed": "2026-01-12T00:00:00+01:00",
                            "last_updated": "2026-01-12T00:00:00+01:00",
                        },
                        {
                            "entity_id": "climate.salong",
                            "state": "heat",
                            "attributes": {"temperature": 19.5},
                            "last_changed": "2026-01-12T00:00:00+01:00",
                            "last_updated": "2026-01-12T06:00:00+01:00",
                        },
                    ]
                ],
            )
        return httpx.Response(
            200,
            json=[
                [
                    {
                        "entity_id": "sensor.salong",
                        "state": "20.8",
                        "last_changed": "2026-01-12T00:00:00+01:00",
                    }
                ]
            ],
        )

    client = HomeAssistantClient(
        HomeAssistantConfig(base_url="http://ha.local", token="t"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    from datetime import datetime

    async with client:
        history = await client.history(
            ["sensor.salong", "climate.salong"], datetime.fromisoformat("2026-01-11T00:00:00+01:00")
        )

    assert [point.value for point in history["climate.salong"]] == [21.0, 19.5]
    # The attribute change is dated by last_updated, when it happened.
    assert history["climate.salong"][1].moment.hour == 6
    assert [point.value for point in history["sensor.salong"]] == [20.8]
    climate_request = next(r for r in seen if "climate" in r.url.params["filter_entity_id"])
    assert "no_attributes" not in climate_request.url.params
    sensor_request = next(r for r in seen if "sensor" in r.url.params["filter_entity_id"])
    assert "no_attributes" in sensor_request.url.params


def test_setpoints_are_read_from_state_attributes():
    from hemopt.ha import climate_setpoints

    rows = [
        {"entity_id": "climate.a", "state": "heat", "attributes": {"temperature": 21.5}},
        {"entity_id": "climate.b", "state": "off", "attributes": {}},
        {"entity_id": "sensor.c", "state": "3", "attributes": {"temperature": 9}},
    ]
    assert climate_setpoints(rows) == {"climate.a": 21.5}
