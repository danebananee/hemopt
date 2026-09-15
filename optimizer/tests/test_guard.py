from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hemopt.config import ExtControlConfig
from hemopt.guard import PeakGuard
from hemopt.peaks import HourAccumulator

TZ = ZoneInfo("Europe/Stockholm")
HOUR = datetime(2026, 1, 12, 8, 0, tzinfo=TZ)


def accumulator_at(minutes: int, average_kw: float) -> HourAccumulator:
    """Accumulator that has banked `average_kw` for `minutes` of the hour."""
    accumulator = HourAccumulator(hour_start=HOUR)
    for minute in range(0, minutes + 1, 5):
        accumulator.add_sample(HOUR + timedelta(minutes=minute), average_kw)
    return accumulator


def guard(**overrides) -> PeakGuard:
    defaults = {"enabled": True, "max_block_minutes": 120, "min_release_minutes": 15}
    return PeakGuard(ExtControlConfig(**(defaults | overrides)))


def evaluate(
    peak_guard: PeakGuard,
    *,
    minutes: int,
    average_kw: float,
    threshold_kw: float = 4.0,
    pump_kw: float = 2.0,
    in_window: bool = True,
    coldest: float | None = 21.0,
    now: datetime | None = None,
):
    return peak_guard.evaluate(
        now=now or HOUR + timedelta(minutes=minutes),
        accumulator=accumulator_at(minutes, average_kw),
        threshold_kw=threshold_kw,
        in_peak_window=in_window,
        heat_pump_kw=pump_kw,
        coldest_room_c=coldest,
    )


def test_disabled_guard_never_blocks():
    decision = evaluate(guard(enabled=False), minutes=30, average_kw=20.0)
    assert decision.block is False


def test_no_block_outside_the_billed_window():
    decision = evaluate(guard(), minutes=30, average_kw=20.0, in_window=False)
    assert decision.block is False
    assert "window" in decision.reason


def test_no_block_while_there_is_budget_left():
    decision = evaluate(guard(), minutes=30, average_kw=1.0)
    assert decision.block is False
    assert decision.allowed_kw > 2.0


def test_blocks_when_the_hour_budget_runs_out():
    # 7 kW for half an hour is 3.5 kWh against a 4 kWh budget, leaving 1 kW
    # of average for the remaining half hour and the pump wants 2 kW.
    decision = evaluate(guard(), minutes=30, average_kw=7.0)
    assert decision.block is True
    assert decision.allowed_kw < 2.0


def test_comfort_overrides_the_tariff():
    decision = evaluate(guard(min_room_temperature=18.0), minutes=30, average_kw=7.0, coldest=17.4)
    assert decision.block is False
    assert "17.4" in decision.reason


def test_block_releases_once_headroom_returns():
    peak_guard = guard()
    assert evaluate(peak_guard, minutes=30, average_kw=7.0).block is True

    # A new hour resets the meter, so the budget is available again.
    later = HOUR + timedelta(hours=1, minutes=5)
    decision = peak_guard.evaluate(
        now=later,
        accumulator=HourAccumulator(hour_start=later.replace(minute=0)),
        threshold_kw=4.0,
        in_peak_window=True,
        heat_pump_kw=2.0,
        coldest_room_c=21.0,
    )
    assert decision.block is False


def test_block_is_held_through_marginal_headroom():
    """Without hysteresis the compressor would chatter around the threshold."""
    peak_guard = guard()
    assert evaluate(peak_guard, minutes=30, average_kw=7.0).block is True

    # Budget recovers to just above the pump's draw, but not by the margin the
    # guard requires before letting go.
    decision = peak_guard.evaluate(
        now=HOUR + timedelta(minutes=40),
        accumulator=accumulator_at(40, 5.55),
        threshold_kw=4.0,
        in_peak_window=True,
        heat_pump_kw=2.0,
        coldest_room_c=21.0,
    )
    assert decision.block is True


def test_block_cannot_exceed_the_configured_cap():
    peak_guard = guard(max_block_minutes=20)
    assert evaluate(peak_guard, minutes=10, average_kw=20.0).block is True

    decision = peak_guard.evaluate(
        now=HOUR + timedelta(minutes=45),
        accumulator=accumulator_at(45, 20.0),
        threshold_kw=4.0,
        in_peak_window=True,
        heat_pump_kw=2.0,
        coldest_room_c=21.0,
    )
    assert decision.block is False
    assert "cap" in decision.reason


def test_minimum_release_time_is_respected():
    peak_guard = guard(min_release_minutes=15)
    evaluate(peak_guard, minutes=10, average_kw=20.0)
    peak_guard.evaluate(
        now=HOUR + timedelta(minutes=20),
        accumulator=accumulator_at(20, 0.1),
        threshold_kw=4.0,
        in_peak_window=True,
        heat_pump_kw=2.0,
        coldest_room_c=21.0,
    )

    # Re-blocking immediately after a release is suppressed.
    decision = peak_guard.evaluate(
        now=HOUR + timedelta(minutes=25),
        accumulator=accumulator_at(25, 20.0),
        threshold_kw=4.0,
        in_peak_window=True,
        heat_pump_kw=2.0,
        coldest_room_c=21.0,
    )
    assert decision.block is False
    assert "release" in decision.reason


def test_idle_heat_pump_is_not_blocked():
    """Blocking a pump that is already off achieves nothing."""
    decision = evaluate(guard(), minutes=30, average_kw=20.0, pump_kw=0.0)
    assert decision.block is False
