from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from hemopt.config import EnergyPriceConfig, PeakWindow
from hemopt.prices import PriceClient

TZ = ZoneInfo("Europe/Stockholm")


def quarter_hour_day(day: date, shape: list[float]) -> list[dict]:
    """Build a feed response with the 96 quarter-hour points used since 2025."""
    rows = []
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    for index in range(96):
        point_start = start + timedelta(minutes=15 * index)
        rows.append(
            {
                "SEK_per_kWh": shape[index % len(shape)],
                "EUR_per_kWh": shape[index % len(shape)] / 11.0,
                "EXR": 11.0,
                "time_start": point_start.isoformat(),
                "time_end": (point_start + timedelta(minutes=15)).isoformat(),
            }
        )
    return rows


def make_client(responses: dict[str, object], **overrides) -> PriceClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = responses.get(request.url.path)
        if payload is None:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    defaults = {
        "vat_rate": 0.25,
        "supplier_markup_ore": 0.0,
        "certificate_ore": 0.0,
        "energy_tax_ore": 0.0,
        "transfer_fee_high_ore": 0.0,
        "transfer_fee_normal_ore": 0.0,
    }
    energy = EnergyPriceConfig(**(defaults | overrides))
    return PriceClient(
        "SE3", energy, PeakWindow(), client=httpx.AsyncClient(transport=transport)
    )


def path_for(day: date) -> str:
    return f"/api/v1/prices/{day.year}/{day.month:02d}-{day.day:02d}_SE3.json"


async def test_series_applies_vat_to_the_spot_price():
    today = date(2026, 1, 12)
    client = make_client({path_for(today): quarter_hour_day(today, [2.0])})

    async with client:
        series = await client.series(datetime(2026, 1, 12, 0, tzinfo=TZ), 4, 15)

    assert series.spot == [2.0] * 4
    assert series.total == pytest.approx([2.5] * 4)


async def test_transfer_fee_switches_between_high_and_normal_load():
    today = date(2026, 1, 12)
    client = make_client(
        {path_for(today): quarter_hour_day(today, [1.0])},
        transfer_fee_high_ore=100.0,
        transfer_fee_normal_ore=0.0,
    )

    async with client:
        night = await client.series(datetime(2026, 1, 12, 3, tzinfo=TZ), 1, 15)
        day = await client.series(datetime(2026, 1, 12, 10, tzinfo=TZ), 1, 15)

    assert night.total[0] == pytest.approx(1.25)
    assert day.total[0] == pytest.approx(1.25 + 1.25)


async def test_quarter_hour_resolution_is_preserved():
    today = date(2026, 1, 12)
    shape = [1.0, 2.0, 3.0, 4.0]
    client = make_client({path_for(today): quarter_hour_day(today, shape)})

    async with client:
        series = await client.series(datetime(2026, 1, 12, 0, tzinfo=TZ), 4, 15)

    assert series.spot == shape


async def test_hourly_feed_is_upsampled_to_quarter_hours():
    today = date(2026, 1, 12)
    start = datetime(2026, 1, 12, 0, tzinfo=TZ)
    hourly = [
        {
            "SEK_per_kWh": float(hour),
            "EUR_per_kWh": 0.0,
            "EXR": 11.0,
            "time_start": (start + timedelta(hours=hour)).isoformat(),
            "time_end": (start + timedelta(hours=hour + 1)).isoformat(),
        }
        for hour in range(24)
    ]
    client = make_client({path_for(today): hourly})

    async with client:
        series = await client.series(start, 8, 15)

    assert series.spot == [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]


async def test_missing_tomorrow_is_extrapolated_from_today():
    today = date(2026, 1, 12)
    client = make_client({path_for(today): quarter_hour_day(today, [1.0, 5.0])})

    async with client:
        series = await client.series(datetime(2026, 1, 12, 22, tzinfo=TZ), 16, 15)

    assert series.is_partly_forecast
    assert series.forecast_from == datetime(2026, 1, 13, 0, tzinfo=TZ)
    assert len(series.spot) == 16
    assert all(value > 0 for value in series.spot)


async def test_published_tomorrow_is_used_directly():
    today = date(2026, 1, 12)
    tomorrow = date(2026, 1, 13)
    client = make_client(
        {
            path_for(today): quarter_hour_day(today, [1.0]),
            path_for(tomorrow): quarter_hour_day(tomorrow, [9.0]),
        }
    )

    async with client:
        series = await client.series(datetime(2026, 1, 12, 23, 30, tzinfo=TZ), 4, 15)

    assert not series.is_partly_forecast
    assert series.spot == [1.0, 1.0, 9.0, 9.0]


async def test_no_prices_at_all_is_an_error():
    client = make_client({})
    with pytest.raises(RuntimeError):
        async with client:
            await client.series(datetime(2026, 1, 12, 0, tzinfo=TZ), 4, 15)


async def test_slice_from_trims_to_the_requested_window():
    today = date(2026, 1, 12)
    client = make_client({path_for(today): quarter_hour_day(today, [1.0, 2.0])})

    async with client:
        series = await client.series(datetime(2026, 1, 12, 0, tzinfo=TZ), 8, 15)

    trimmed = series.slice_from(datetime(2026, 1, 12, 0, 30, tzinfo=TZ), 2)
    assert len(trimmed) == 2
    assert trimmed.times[0] == datetime(2026, 1, 12, 0, 30, tzinfo=TZ)
