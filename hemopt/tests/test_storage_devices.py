"""Battery and car as the engine reads them from Home Assistant."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hemopt.demo import DemoEngine, demo_config

TZ = ZoneInfo("Europe/Stockholm")


@pytest.fixture
def engine(tmp_path):
    config = demo_config(database_path=str(tmp_path / "d.db"))
    config.battery.enabled = True
    config.battery.soc_entity = "sensor.battery_soc"
    config.ev.enabled = True
    config.ev.soc_entity = "sensor.car_soc"
    config.ev.plugged_entity = "binary_sensor.car_plugged"
    config.ev.departure_time = "07:00"
    return DemoEngine(config)


def grid(start: datetime, steps: int = 144) -> list[datetime]:
    return [start + timedelta(minutes=15 * i) for i in range(steps)]


def test_battery_charge_is_read_as_energy(engine):
    battery = engine._battery_input({"sensor.battery_soc": "40"})
    assert battery.initial_kwh == pytest.approx(4.0)
    assert battery.min_kwh == pytest.approx(1.0)
    # Unknown charge: plan from the middle rather than not at all.
    assert engine._battery_input({}).initial_kwh == pytest.approx(5.5)


def test_the_car_must_be_ready_at_the_next_departure(engine):
    times = grid(datetime(2026, 1, 12, 18, 0, tzinfo=TZ))
    states = {"sensor.car_soc": "30", "binary_sensor.car_plugged": "on"}
    ev = engine._ev_input(states, times)
    assert ev.initial_kwh == pytest.approx(18.0)
    assert ev.target_kwh == pytest.approx(48.0)
    # 18:00 to 07:00 is 13 hours, 52 quarters; the last one ends at 07:00.
    assert ev.deadline_step == 51
    assert all(ev.available[:52]) and not any(ev.available[52:])


@pytest.mark.parametrize("state", ["off", "disconnected", "unavailable", ""])
def test_an_unplugged_car_is_left_out(engine, state):
    times = grid(datetime(2026, 1, 12, 18, 0, tzinfo=TZ))
    assert engine._ev_input({"binary_sensor.car_plugged": state}, times) is None


@pytest.mark.parametrize("state", ["on", "charging", "awaiting_start", "completed", "connected"])
def test_charger_states_that_mean_plugged_in(engine, state):
    assert engine.ev_plugged({"binary_sensor.car_plugged": state})
