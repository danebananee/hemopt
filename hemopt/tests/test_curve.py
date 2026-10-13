"""The heating-curve tuner: need from room behaviour, and small safe steps."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hemopt.curve import (
    TARGET_DUTY,
    CurveRoom,
    HourSample,
    Knob,
    RoomHour,
    analyse,
    build_hours,
    learn_gain,
    propose,
    room_need,
)

TZ = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=TZ)
ROOMS = [CurveRoom("salong", "Salong", 1), CurveRoom("sovrum", "Sovrum", 2)]


def true_need(key: str, outdoor: float) -> float:
    """Supply each room needs at TARGET_DUTY: Salong is the demanding one."""
    slope = {"salong": 0.55, "sovrum": 0.40}[key]
    return 21.0 + slope * (20.0 - outdoor)


def room_response(key: str, outdoor: float, supply: float, setpoint: float = 21.0) -> RoomHour:
    """What a thermostat-controlled floor loop does at this supply temperature."""
    need = true_need(key, outdoor)
    k = TARGET_DUTY * (need - setpoint) / (setpoint - outdoor)
    if supply >= need:
        duty = TARGET_DUTY * (need - setpoint) / (supply - setpoint)
        return RoomHour(setpoint, setpoint, duty)
    temperature = (supply + k * outdoor) / (1 + k)
    return RoomHour(temperature, setpoint, 1.0)


def house(curve_offset: float, days: int = 14, outdoor_mid: float = 6.0) -> list[HourSample]:
    hours = []
    for index in range(days * 24):
        moment = NOW - timedelta(hours=days * 24 - index)
        outdoor = outdoor_mid + 4.0 * math.sin(2 * math.pi * index / 24) - 4.0 * index / (days * 24)
        supply = true_need("salong", outdoor) + curve_offset
        sample = HourSample(moment=moment, outdoor=outdoor, supply=supply, target=supply)
        for room in ROOMS:
            sample.rooms[room.key] = room_response(room.key, outdoor, supply)
        hours.append(sample)
    return hours


def test_room_need_inverts_the_room_response():
    for supply in (25.0, 30.0, 40.0):
        hour = room_response("salong", 5.0, supply)
        need = room_need(
            outdoor=5.0,
            supply=supply,
            temperature=hour.temperature,
            setpoint=hour.setpoint,
            duty=hour.duty,
        )
        assert need == pytest.approx(true_need("salong", 5.0), abs=0.05)


@pytest.mark.parametrize("offset,status", [(-3.0, "too_cold"), (4.0, "too_warm"), (0.0, "ok")])
def test_analysis_finds_how_far_the_curve_is_off(offset, status):
    report = analyse(house(offset), ROOMS, now=NOW, since=None)
    assert report.status == status
    assert report.shift_c == pytest.approx(-offset, abs=0.4)
    assert {band["limiting"] for band in report.bands} == {"Salong"}


def test_slope_is_learnt_when_the_weather_spans_enough():
    report = analyse(house(0.0), ROOMS, now=NOW, since=None)
    assert report.need_line[1] == pytest.approx(-0.55, abs=0.05)


def test_no_warm_water_is_reported_instead_of_a_curve_change():
    hours = house(-3.0)
    for hour in hours:
        hour.supply = 20.5
        hour.target = None
        for key in hour.rooms:
            hour.rooms[key] = RoomHour(19.5, 21.0, 1.0)
    report = analyse(hours, ROOMS, now=NOW, since=None)
    assert report.status == "no_heat"
    assert "sommarläge" in report.detail


def test_a_room_far_beyond_the_rest_is_reported_not_followed():
    rooms = [*ROOMS, CurveRoom("garderob", "Garderob", 1)]
    hours = house(0.0)
    for hour in hours:
        # A loop that barely reaches its room: open all the time, still cold.
        hour.rooms["garderob"] = RoomHour(17.0, 21.0, 1.0)
    report = analyse(hours, rooms, now=NOW, since=None)
    assert "Garderob" in report.suspects
    assert report.status == "ok"


def test_flexible_rooms_do_not_set_the_curve():
    rooms = [CurveRoom("salong", "Salong", 3), CurveRoom("sovrum", "Sovrum", 2)]
    report = analyse(house(0.0), rooms, now=NOW, since=None)
    assert report.status == "too_warm"
    assert {band["limiting"] for band in report.bands} == {"Sovrum"}


def test_too_little_data_waits():
    report = analyse(house(0.0, days=1)[:20], ROOMS, now=NOW, since=None)
    assert report.status == "need_data"


def test_proposals_are_small_bounded_steps():
    report = analyse(house(-4.0), ROOMS, now=NOW, since=None)
    fine = Knob("number.curve_fine", 0.0, -10.0, 10.0, 0.1, gain=1.0)
    first = propose(report, fine=fine, curve=None, original={}, max_daily_c=1.5, max_total_c=6.0)
    assert first.entity == "number.curve_fine"
    assert first.new == pytest.approx(1.5)
    # Near the end of the budget the step stops at it.
    fine.value = 5.5
    capped = propose(
        report, fine=fine, curve=None, original={fine.entity: 0.0}, max_daily_c=1.5, max_total_c=6.0
    )
    assert capped.new == pytest.approx(6.0)
    fine.value = 6.0
    assert (
        propose(
            report,
            fine=fine,
            curve=None,
            original={fine.entity: 0.0},
            max_daily_c=1.5,
            max_total_c=6.0,
        )
        is None
    )


def test_an_unmeasured_setting_moves_one_step_first():
    report = analyse(house(3.0), ROOMS, now=NOW, since=None)
    curve = Knob("number.curve", 4.0, 0.0, 10.0, 0.1, gain=None)
    step = propose(report, fine=None, curve=curve, original={}, max_daily_c=1.5, max_total_c=6.0)
    assert step.new == pytest.approx(3.9)


def test_nothing_is_proposed_when_the_curve_is_right():
    report = analyse(house(0.0), ROOMS, now=NOW, since=None)
    fine = Knob("number.curve_fine", 0.0, -10.0, 10.0, 0.1, gain=1.0)
    assert (
        propose(report, fine=fine, curve=None, original={}, max_daily_c=1.5, max_total_c=6.0)
        is None
    )


def test_gain_is_measured_from_the_curves_own_target():
    gain = learn_gain(
        before=30.0, after=31.6, outdoor_before=5.0, outdoor_after=6.0, slope=-0.4, delta_units=1.0
    )
    assert gain == pytest.approx(2.0)
    assert (
        learn_gain(
            before=30.0,
            after=30.0,
            outdoor_before=5.0,
            outdoor_after=5.0,
            slope=0.0,
            delta_units=1.0,
        )
        is None
    )


def test_hours_are_built_from_sparse_readings():
    start = NOW - timedelta(hours=3)
    outdoor = [(start + timedelta(minutes=10 * i), 5.0) for i in range(18)]
    supply = [(start + timedelta(minutes=10 * i), 30.0) for i in range(18)]
    temps = [(start + timedelta(minutes=5 * i), 20.8) for i in range(36)]
    setpoints = [(start - timedelta(days=1), 21.0)]
    hours = build_hours(
        outdoor=outdoor,
        supply=supply,
        target=[],
        rooms={"salong": (temps, setpoints)},
        start=start,
        end=NOW,
    )
    assert len(hours) == 3
    assert hours[0].rooms["salong"].duty == pytest.approx(1.0)
    assert hours[0].target is None


def test_daily_steps_converge_without_overshooting():
    """A curve 4 °C too low, a fine setting worth 2 °C per unit, unknown at first."""
    true_gain = 2.0
    offset_units = 0.0
    knob = Knob("number.curve_fine", 0.0, -10.0, 10.0, 0.1, gain=None)
    history: list[HourSample] = []
    statuses = []
    for day in range(12):
        today = NOW + timedelta(days=day)
        supply_offset = -4.0 + true_gain * offset_units
        for hour in range(24):
            moment = today - timedelta(hours=24 - hour)
            outdoor = 5.0 + 3.0 * math.sin(2 * math.pi * hour / 24)
            supply = true_need("salong", outdoor) + supply_offset
            sample = HourSample(moment=moment, outdoor=outdoor, supply=supply, target=supply)
            for room in ROOMS:
                sample.rooms[room.key] = room_response(room.key, outdoor, supply)
            history.append(sample)
        since = today - timedelta(hours=24)
        report = analyse(history[-14 * 24 :], ROOMS, now=today, since=since)
        statuses.append(report.status)
        knob.value = offset_units
        step = propose(
            report,
            fine=knob,
            curve=None,
            original={knob.entity: 0.0},
            max_daily_c=1.5,
            max_total_c=6.0,
        )
        if step is None:
            continue
        delta = step.new - offset_units
        if knob.gain is None:
            knob.gain = learn_gain(
                before=0.0,
                after=true_gain * delta,
                outdoor_before=5.0,
                outdoor_after=5.0,
                slope=0.0,
                delta_units=delta,
            )
        offset_units = step.new
    assert statuses[0] == "too_cold"
    assert statuses[-1] == "ok"
    assert "too_warm" not in statuses
    assert abs(-4.0 + true_gain * offset_units) <= 0.8


def test_a_curve_changed_by_hand_is_read_as_it_is_now():
    hours = house(-3.0)
    for hour in hours[-48:]:
        hour.target = true_need("salong", hour.outdoor) + 2.0
    report = analyse(hours, ROOMS, now=NOW, since=None)
    a, b = report.current_line
    assert a + b * 5.0 == pytest.approx(true_need("salong", 5.0) + 2.0, abs=0.3)


def test_points_start_from_a_reasonable_floor_heating_curve():
    from hemopt.curve import CurveReport, recommend_points

    points = recommend_points(CurveReport())
    assert (points.foot_c, points.end_c, points.basis) == (20.0, 32.0, "default")
    assert points.at(0.0) == pytest.approx(26.0)


def test_points_follow_the_measured_need():
    from hemopt.curve import recommend_points

    report = analyse(house(-3.0), ROOMS, now=NOW, since=None)
    points = recommend_points(report)
    assert points.basis == "measured"
    # Salong needs 21 + 0.55 * (20 - outdoor); the end point is capped for floor heating.
    assert points.foot_c == pytest.approx(21.0, abs=0.7)
    assert points.end_c == pytest.approx(40.0)


def test_narrow_weather_keeps_the_usual_shape_at_the_measured_level():
    from hemopt.curve import recommend_points

    hours = house(-3.0, outdoor_mid=8.0)
    for hour in hours:
        hour.outdoor = 8.0 + 0.5 * math.sin(hour.moment.timestamp() / 3600)
        hour.supply = hour.target = true_need("salong", hour.outdoor) - 3.0
        for room in ROOMS:
            hour.rooms[room.key] = room_response(room.key, hour.outdoor, hour.supply)
    report = analyse(hours, ROOMS, now=NOW, since=None)
    points = recommend_points(report)
    assert points.basis == "shifted"
    assert points.at(8.0) == pytest.approx(true_need("salong", 8.0), abs=0.6)
    assert points.end_c - points.foot_c == pytest.approx(12.0, abs=0.1)


def test_points_stay_within_floor_heating_limits():
    from hemopt.curve import CurveReport, recommend_points

    report = CurveReport(status="too_cold", need_line=(60.0, -2.0), slope_known=True, span_c=10.0)
    points = recommend_points(report)
    assert points.foot_c <= 28.0
    assert points.end_c <= 40.0


def test_a_room_setpoint_can_shift_the_curve():
    report = analyse(house(-3.0), ROOMS, now=NOW, since=None)
    room = Knob("climate.h66_room", 20.0, 10.0, 30.0, 0.5, gain=None, label="rumsbörvärdet")
    step = propose(report, fine=room, curve=None, original={}, max_daily_c=1.5, max_total_c=6.0)
    assert step.new == pytest.approx(20.5)
    assert "rumsbörvärdet" in step.reason


def test_shape_is_written_once_then_only_when_measured_and_weekly(tmp_path):
    from hemopt.curve import CurvePoints
    from hemopt.demo import DemoEngine, demo_config

    engine = DemoEngine(demo_config(database_path=str(tmp_path / "t.db")))
    foot = Knob("number.foot", 10.0, 0.0, 40.0, 0.5)
    end = Knob("number.end", 28.7, 0.0, 60.0, 0.5)
    engine.curve_points = CurvePoints(20.0, 32.0, -20.0, "default")
    assert engine._points_due({}, foot, end, NOW)
    state = {"points_at": NOW.isoformat()}
    assert not engine._points_due(state, foot, end, NOW + timedelta(days=10))
    engine.curve_points = CurvePoints(22.0, 35.0, -20.0, "measured")
    assert not engine._points_due(state, foot, end, NOW + timedelta(days=3))
    assert engine._points_due(state, foot, end, NOW + timedelta(days=8))
    assert not engine._points_due(state, None, end, NOW + timedelta(days=8))


def test_a_pump_that_ignores_its_curve_is_reported_not_tuned():
    # What a real house showed in October: the curve asks for 30 °C, the pump
    # only makes hot water, and the floor water stays at 21 °C.
    hours = []
    for index in range(36):
        moment = NOW - timedelta(hours=36 - index)
        sample = HourSample(moment=moment, outdoor=6.0, supply=21.0, target=30.0)
        for room in ROOMS:
            sample.rooms[room.key] = RoomHour(18.0, 21.0, 1.0)
        hours.append(sample)
    report = analyse(hours, ROOMS, now=NOW, since=None)
    assert report.status == "no_heat"
    assert "30 °C" in report.detail and "21 °C" in report.detail


def test_a_curve_changed_by_hand_is_read_from_after_the_change():
    from hemopt.curve import curve_changed_at

    hours = house(curve_offset=0.0, days=4)
    jump_at = hours[60].moment
    for h in hours[60:]:
        h.target = h.target + 8.0
    assert curve_changed_at(hours) == jump_at
    report = analyse(hours, ROOMS, now=NOW, since=None)
    level_now = report.current_line[0] + report.current_line[1] * 5.0
    assert level_now == pytest.approx(true_need("salong", 5.0) + 8.0, abs=1.5)


def test_an_impossible_slope_from_little_weather_is_not_believed():
    from hemopt.curve import SLOPE_RANGE, _current_line

    # The target ramping up while it gets a little colder looks like -3 °C/°C.
    points = [(NOW - timedelta(hours=20 - i), 9.0 - 0.25 * i, 25.0 + 0.75 * i) for i in range(20)]
    line = _current_line(points, NOW)
    assert SLOPE_RANGE[0] <= line[1] <= SLOPE_RANGE[1]
    with_known = _current_line(points, NOW, known_slope=-0.84)
    assert with_known[1] == -0.84


def test_the_supply_ignores_a_hot_water_spike():
    start = NOW - timedelta(hours=1)
    supply = [(start + timedelta(minutes=5 * i), 50.0 if i in (3, 4) else 30.0) for i in range(12)]
    hours = build_hours(
        outdoor=[(start, 5.0)],
        supply=supply,
        target=[(start, 30.0)],
        rooms={},
        start=start,
        end=NOW,
    )
    assert hours[0].supply == pytest.approx(30.0)
