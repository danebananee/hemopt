"""Rebuilding lost consumption history from Home Assistant's own records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hemopt.config import HomeAssistantConfig
from hemopt.engine import hourly_means
from hemopt.ha import HomeAssistantClient, parse_statistics, wind_to_ms
from hemopt.storage import Store

TZ = ZoneInfo("Europe/Stockholm")


def test_statistics_rows_accept_epoch_milliseconds_and_iso():
    start = datetime(2026, 9, 1, 10, tzinfo=UTC)
    rows = parse_statistics(
        {
            "sensor.p1": [
                {"start": start.timestamp() * 1000, "mean": 1.5},
                {"start": (start + timedelta(hours=1)).isoformat(), "mean": 2.0},
                {"start": start.timestamp() * 1000, "mean": None},
            ]
        },
        "sensor.p1",
    )
    assert rows == [(start, 1.5), (start + timedelta(hours=1), 2.0)]


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://supervisor/core", "ws://supervisor/core/websocket"),
        ("http://homeassistant.local:8123", "ws://homeassistant.local:8123/api/websocket"),
        ("https://ha.example.se/api", "wss://ha.example.se/api/websocket"),
    ],
)
def test_websocket_url_follows_the_rest_url(base, expected):
    client = HomeAssistantClient(HomeAssistantConfig(base_url=base, token="t"))
    assert client.websocket_url() == expected


def test_hourly_means_weigh_each_reading_by_how_long_it_held():
    hour = datetime(2026, 9, 1, 10, tzinfo=TZ)
    points = [
        (hour, 1.0),
        (hour + timedelta(minutes=30), 3.0),
        (hour + timedelta(minutes=90), 2.0),
    ]
    means = dict(hourly_means(points, until=hour + timedelta(hours=2)))
    assert means[hour] == pytest.approx(2.0)
    assert means[hour + timedelta(hours=1)] == pytest.approx(2.5)


def test_only_missing_or_zeroed_hours_are_filled(tmp_path):
    store = Store(tmp_path / "h.db")
    hour = datetime(2026, 9, 1, 10, tzinfo=TZ)
    store.record_hourly_power(hour, 0.0)  # what the old bug wrote
    store.record_hourly_power(hour + timedelta(hours=1), 1.7)  # a real measurement
    written = store.fill_hourly_power(
        [(hour, 2.1), (hour + timedelta(hours=1), 9.9), (hour + timedelta(hours=2), 1.2)]
    )
    stored = store.all_hourly_power()
    assert written == 2
    assert stored[hour] == pytest.approx(2.1)
    assert stored[hour + timedelta(hours=1)] == pytest.approx(1.7)
    assert stored[hour + timedelta(hours=2)] == pytest.approx(1.2)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [(36.0, "km/h", 10.0), (10.0, "m/s", 10.0), (10.0, None, 10.0), (10.0, "kn", 5.14444)],
)
def test_wind_is_converted_to_metres_per_second(value, unit, expected):
    assert wind_to_ms(value, unit) == pytest.approx(expected, rel=1e-4)
