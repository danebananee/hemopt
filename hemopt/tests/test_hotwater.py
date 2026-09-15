from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hemopt.config import HotWaterConfig
from hemopt.hotwater import (
    DrawEvent,
    TankSample,
    UsageProfile,
    build_profile,
    estimate_draws,
)

TZ = ZoneInfo("Europe/Stockholm")


def config() -> HotWaterConfig:
    return HotWaterConfig(tank_litres=180.0, standing_loss_kwh_per_day=1.2)


def test_standing_loss_alone_is_not_a_draw():
    start = datetime(2026, 1, 12, 2, tzinfo=TZ)
    settings = config()
    loss_per_hour = settings.standing_loss_kwh_per_day / 24.0
    drop_per_hour = loss_per_hour / settings.kwh_per_degree

    samples = [
        TankSample(start + timedelta(hours=h), 55.0 - drop_per_hour * h, charging=False)
        for h in range(6)
    ]

    assert estimate_draws(samples, settings) == []


def test_a_shower_is_detected():
    start = datetime(2026, 1, 12, 7, tzinfo=TZ)
    samples = [
        TankSample(start, 55.0, charging=False),
        TankSample(start + timedelta(minutes=15), 48.0, charging=False),
    ]

    events = estimate_draws(samples, config())

    assert len(events) == 1
    # 7 K out of a 180 litre tank is roughly 1.5 kWh.
    assert 1.3 < events[0].kwh < 1.7


def test_charging_periods_are_ignored():
    start = datetime(2026, 1, 12, 7, tzinfo=TZ)
    samples = [
        TankSample(start, 55.0, charging=True),
        TankSample(start + timedelta(minutes=15), 48.0, charging=False),
    ]

    assert estimate_draws(samples, config()) == []


def test_rising_temperature_is_not_a_draw():
    start = datetime(2026, 1, 12, 7, tzinfo=TZ)
    samples = [
        TankSample(start, 48.0, charging=False),
        TankSample(start + timedelta(minutes=15), 55.0, charging=False),
    ]

    assert estimate_draws(samples, config()) == []


def test_default_profile_has_a_morning_and_an_evening_peak():
    profile = UsageProfile.default()
    weekday_hours = {hour: profile.grid[(2, hour)] for hour in range(24)}

    assert weekday_hours[7] > weekday_hours[3]
    assert weekday_hours[20] > weekday_hours[14]


def test_expected_kwh_spans_hour_boundaries():
    profile = UsageProfile(grid={(0, 7): 1.0, (0, 8): 3.0})
    moment = datetime(2026, 1, 12, 7, 30, tzinfo=TZ)

    # Half an hour at 1.0 kWh/h plus half an hour at 3.0 kWh/h.
    assert profile.expected_kwh(moment, 1.0) == 2.0


def test_build_profile_learns_the_observed_shape():
    events = [
        DrawEvent(datetime(2026, 1, 12, 7, tzinfo=TZ) + timedelta(days=7 * week), 2.0)
        for week in range(3)
    ]
    profile = build_profile(events, days_observed=21.0, prior=UsageProfile.default())

    assert profile.fitted
    assert profile.grid[(0, 7)] > profile.grid[(0, 3)]


def test_build_profile_keeps_the_prior_without_enough_history():
    prior = UsageProfile.default()
    profile = build_profile([DrawEvent(datetime.now(TZ), 1.0)], days_observed=1.0, prior=prior)

    assert not profile.fitted
    assert profile.grid == prior.grid


def test_busiest_hours_reports_the_peak_taps():
    profile = UsageProfile(grid={(0, hour): float(hour) for hour in range(24)})
    assert [hour for hour, _ in profile.busiest_hours(0, count=2)] == [23, 22]
