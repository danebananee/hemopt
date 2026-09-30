"""Sun, wind and wood stove: learnt from the house, not configured."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hemopt.thermal import ThermalModel, ThermalSample, identify
from hemopt.weather import solar_elevation, sun_factor
from hemopt.woodstove import (
    FireDetector,
    RoomExcess,
    StoveSession,
    fire_signal,
    lit_at,
    open_session,
    session_totals,
    sessions_from_setting,
    sessions_to_setting,
)

TZ = ZoneInfo("Europe/Stockholm")
STOCKHOLM = (59.33, 18.07)


def test_the_sun_is_where_it_should_be_over_stockholm():
    midsummer = datetime(2026, 6, 21, 13, 20, tzinfo=TZ)
    midwinter = datetime(2026, 12, 21, 12, 0, tzinfo=TZ)
    assert solar_elevation(midsummer, *STOCKHOLM) == pytest.approx(54.1, abs=1.0)
    assert solar_elevation(midwinter, *STOCKHOLM) == pytest.approx(7.2, abs=1.0)
    assert sun_factor(datetime(2026, 1, 1, 2, tzinfo=TZ), *STOCKHOLM) == 0.0


def test_clouds_dim_the_sun_and_no_position_means_no_sun():
    noon = datetime(2026, 3, 20, 12, 15, tzinfo=TZ)
    clear = sun_factor(noon, *STOCKHOLM, cloud_pct=0)
    overcast = sun_factor(noon, *STOCKHOLM, cloud_pct=100)
    assert clear > 0.45
    assert overcast == pytest.approx(clear * 0.25)
    assert sun_factor(noon, None, None, 0) == 0.0


def _weather_history(truth: ThermalModel, days: int = 28) -> list[ThermalSample]:
    rng = random.Random(11)
    moment = datetime(2026, 3, 1, tzinfo=TZ)
    indoor, loop = 21.0, 0.5
    cloud, wind = 50.0, 3.0
    samples = []
    for index in range(96 * days):
        outdoor = 2.0 + 5.0 * math.sin((index / 96 - 0.4) * 2 * math.pi)
        if index % 24 == 0:
            loop = rng.choice([0.0, 0.4, 0.8])
        if index % 32 == 0:
            cloud = rng.choice([0.0, 30.0, 90.0, 100.0])
            wind = rng.choice([0.0, 2.0, 6.0, 12.0])
        sun = sun_factor(moment, *STOCKHOLM, cloud)
        samples.append(
            ThermalSample(moment, indoor + rng.gauss(0, 0.02), outdoor, loop, sun=sun, wind_ms=wind)
        )
        indoor, _ = truth.advance(indoor, loop, outdoor, loop, 0.25, sun=sun, wind_ms=wind)
        moment += timedelta(minutes=15)
    return samples


def test_sun_and_wind_are_learnt_per_room():
    truth = ThermalModel(70.0, 0.6, 0.01, 1.0, 0, True, k_sun_per_hour=0.8, k_wind_per_hour=0.002)
    fitted = identify(_weather_history(truth), floor_type="light")
    assert fitted.k_sun_per_hour == pytest.approx(0.8, rel=0.2)
    assert fitted.k_wind_per_hour == pytest.approx(0.002, rel=0.3)
    assert fitted.tau_hours == pytest.approx(70.0, rel=0.15)
    assert fitted.history_days == pytest.approx(28.0, abs=0.5)


def test_a_house_without_weather_data_learns_no_weather():
    truth = ThermalModel(70.0, 0.6, 0.01, 1.0, 0, True)
    samples = [
        ThermalSample(s.moment, s.indoor, s.outdoor, s.heat_fraction)
        for s in _weather_history(truth, days=14)
    ]
    fitted = identify(samples, floor_type="light")
    assert fitted.k_sun_per_hour == 0.0
    assert fitted.k_wind_per_hour == 0.0


def test_recent_behaviour_outweighs_old():
    """A room that changed (new windows, say) follows the new behaviour."""
    old = ThermalModel(40.0, 0.6, 0.0, 1.0, 0, True)
    new = ThermalModel(90.0, 0.6, 0.0, 1.0, 0, True)
    early = _weather_history(old, days=30)
    late = _weather_history(new, days=30)
    shift = early[-1].moment - late[0].moment + timedelta(minutes=15)
    combined = early + [
        ThermalSample(s.moment + shift, s.indoor, s.outdoor, s.heat_fraction, sun=s.sun)
        for s in late
    ]
    fitted = identify(combined, floor_type="light")
    assert fitted.tau_hours > 65.0


def test_marked_fires_round_trip_and_close_themselves():
    start = datetime(2026, 1, 10, 18, tzinfo=TZ)
    sessions = [
        StoveSession(start, start + timedelta(hours=2)),
        StoveSession(start + timedelta(days=1)),
    ]
    restored = sessions_from_setting(sessions_to_setting(sessions))
    assert restored == sessions
    assert lit_at(restored, start + timedelta(hours=1), 4.0)
    assert not lit_at(restored, start + timedelta(hours=3), 4.0)
    # An unmarked end is assumed after the default burn time.
    assert open_session(restored, start + timedelta(days=1, hours=3), 4.0) is restored[1]
    assert open_session(restored, start + timedelta(days=1, hours=5), 4.0) is None
    count, hours = session_totals(restored, start + timedelta(days=2), 4.0)
    assert (count, hours) == (2, pytest.approx(6.0))


def test_unexplained_warming_is_read_as_a_fire():
    full = [RoomExcess(0.8, observed_rise=0.6, predicted_rise=0.0, hours=0.75)]
    none = [RoomExcess(0.8, observed_rise=0.05, predicted_rise=0.05, hours=0.75)]
    assert fire_signal(full) == pytest.approx(1.0)
    assert fire_signal(none) == pytest.approx(0.0)
    assert fire_signal([]) is None


def test_the_detector_needs_a_sustained_signal_both_ways():
    detector = FireDetector()
    assert detector.update(0.9) is False
    assert detector.update(0.9) is True
    assert detector.update(0.1) is True
    assert detector.update(0.1) is True
    assert detector.update(0.1) is False
    assert detector.update(None) is False


def test_marking_a_fire_through_the_engine(tmp_path):
    from hemopt.demo import DemoEngine, demo_config

    engine = DemoEngine(demo_config(database_path=str(tmp_path / "s.db")))
    report = engine.mark_stove(True)
    assert report.reading.lit and report.reading.source == "manual"
    assert report.enabled, "marking a fire is enough to switch the stove features on"
    assert len(engine.stove_sessions) == 1
    engine.mark_stove(True)  # pressing twice does not start a second fire
    assert len(engine.stove_sessions) == 1
    report = engine.mark_stove(False)
    assert not report.reading.lit
    assert engine.stove_sessions[0].end is not None
    # Sessions survive a restart.
    again = DemoEngine(engine.config, store=engine.store)
    assert len(again.stove_sessions) == 1


def test_savings_sensors_are_always_numbers(tmp_path):
    from hemopt.demo import DemoEngine, demo_config

    engine = DemoEngine(demo_config(database_path=str(tmp_path / "s.db")))
    sensors = engine.savings_sensors()
    assert set(sensors) == {
        "saving_today",
        "saving_month",
        "saving_month_contract",
        "saving_month_control",
        "saving_per_day",
    }
    assert all(isinstance(value, float) for value in sensors.values())
