from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from hemopt.config import HomeAssistantConfig
from hemopt.ha import ForecastPoint, HomeAssistantClient, resample_forecast

TZ = ZoneInfo("Europe/Stockholm")
START = datetime(2026, 1, 12, 0, tzinfo=TZ)


def grid(steps: int, step_minutes: int = 15) -> list[datetime]:
    return [START + timedelta(minutes=step_minutes * i) for i in range(steps)]


def test_missing_forecast_holds_the_measured_value():
    assert resample_forecast([], grid(4), fallback=-3.5) == [-3.5] * 4


def test_forecast_is_interpolated_between_hours():
    points = [
        ForecastPoint(START, -8.0),
        ForecastPoint(START + timedelta(hours=1), -4.0),
    ]
    values = resample_forecast(points, grid(5), fallback=0.0)

    assert values == pytest.approx([-8.0, -7.0, -6.0, -5.0, -4.0])


def test_forecast_holds_flat_beyond_its_last_point():
    points = [ForecastPoint(START, -8.0), ForecastPoint(START + timedelta(hours=1), -4.0)]
    values = resample_forecast(points, grid(8), fallback=0.0)

    assert values[-1] == -4.0
    assert values[5] == -4.0


def test_forecast_holds_flat_before_its_first_point():
    points = [ForecastPoint(START + timedelta(hours=2), -4.0)]
    values = resample_forecast(points, grid(4), fallback=99.0)

    assert values == [-4.0] * 4


async def test_weather_forecast_parses_the_service_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("return_response") == "true"
        return httpx.Response(
            200,
            json={
                "service_response": {
                    "weather.home": {
                        "forecast": [
                            {"datetime": START.isoformat(), "temperature": -6.0},
                            {
                                "datetime": (START + timedelta(hours=1)).isoformat(),
                                "temperature": -5.0,
                            },
                        ]
                    }
                }
            },
        )

    client = HomeAssistantClient(
        HomeAssistantConfig(base_url="http://ha", token="t"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ha"),
    )
    async with client:
        points = await client.weather_forecast("weather.home")

    assert [p.temperature for p in points] == [-6.0, -5.0]


async def test_weather_forecast_survives_an_unsupported_integration():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="not supported")

    client = HomeAssistantClient(
        HomeAssistantConfig(base_url="http://ha", token="t"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ha"),
    )
    async with client:
        assert await client.weather_forecast("weather.home") == []
