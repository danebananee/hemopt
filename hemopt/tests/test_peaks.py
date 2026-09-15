from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hemopt.config import PeakTariffConfig, PeakWindow
from hemopt.peaks import HourAccumulator, daily_maxima, peak_state

TZ = ZoneInfo("Europe/Stockholm")


def tariff(**kwargs) -> PeakTariffConfig:
    defaults = {
        "n_peaks": 5,
        "price_per_kw_sek": 67.5,
        "window": PeakWindow(months=[1, 2, 3, 11, 12], hour_start=7, hour_end=21),
    }
    return PeakTariffConfig(**(defaults | kwargs))


def test_marginal_price_divides_by_the_number_of_counted_peaks():
    assert tariff().marginal_price_per_kw == 67.5 / 5


def test_only_one_peak_per_day_is_counted():
    hourly = {
        datetime(2026, 1, 12, 8, tzinfo=TZ): 5.0,
        datetime(2026, 1, 12, 18, tzinfo=TZ): 7.0,
        datetime(2026, 1, 13, 9, tzinfo=TZ): 6.0,
    }
    peaks = daily_maxima(hourly, tariff())

    assert [p.kw for p in peaks] == [7.0, 6.0]
    assert peaks[0].hour == 18


def test_hours_outside_the_window_are_free():
    hourly = {
        datetime(2026, 1, 12, 3, tzinfo=TZ): 9.0,  # night
        datetime(2026, 1, 17, 10, tzinfo=TZ): 8.5,  # Saturday
        datetime(2026, 1, 12, 8, tzinfo=TZ): 4.0,  # billable
    }
    peaks = daily_maxima(hourly, tariff())

    assert [p.kw for p in peaks] == [4.0]


def test_summer_months_are_free():
    hourly = {datetime(2026, 7, 14, 10, tzinfo=TZ): 9.0}
    assert daily_maxima(hourly, tariff()) == []


def test_new_years_day_is_treated_as_a_holiday():
    hourly = {datetime(2026, 1, 1, 10, tzinfo=TZ): 9.0}
    assert daily_maxima(hourly, tariff()) == []


def test_good_friday_is_excluded():
    # Easter Sunday 2026 falls on 5 April, so Good Friday is 3 April. It is
    # outside the winter window, so March 2026 is used to exercise the rule.
    march_window = tariff(window=PeakWindow(months=[3, 4], hour_start=7, hour_end=21))
    hourly = {datetime(2026, 4, 3, 10, tzinfo=TZ): 9.0}
    assert daily_maxima(hourly, march_window) == []


def test_threshold_is_the_nth_highest_once_the_month_has_enough_days():
    # 12-16 January 2026 is a plain Monday-to-Friday week with no holidays.
    hourly = {
        datetime(2026, 1, day, 10, tzinfo=TZ): value
        for day, value in zip(range(12, 17), [8.0, 7.0, 6.0, 5.0, 4.0], strict=True)
    }
    state = peak_state(hourly, tariff(), expected_peak_kw=2.0)

    assert state.threshold_kw == 4.0
    assert state.average_kw == 6.0
    assert round(state.projected_cost_sek) == round(6.0 * 67.5)


def test_a_holiday_inside_the_week_drops_out_of_the_average():
    """Epiphany falls on a Tuesday in 2026 and must not be billed."""
    hourly = {
        datetime(2026, 1, day, 10, tzinfo=TZ): value
        for day, value in zip(range(5, 10), [8.0, 7.0, 6.0, 5.0, 4.0], strict=True)
    }
    state = peak_state(hourly, tariff(), expected_peak_kw=2.0)

    assert [p.kw for p in state.counted] == [8.0, 6.0, 5.0, 4.0]
    assert state.average_kw == 23.0 / 5


def test_threshold_falls_back_to_the_estimate_early_in_the_month():
    hourly = {datetime(2026, 1, 5, 10, tzinfo=TZ): 3.0}
    state = peak_state(hourly, tariff(), expected_peak_kw=5.5)

    assert state.threshold_kw == 5.5, "one measured day must not set the month's threshold"


def test_empty_month_uses_the_estimate():
    state = peak_state({}, tariff(), expected_peak_kw=4.2)
    assert state.threshold_kw == 4.2
    assert state.average_kw == 0.0


def test_accumulator_integrates_energy_over_the_hour():
    start = datetime(2026, 1, 12, 8, 0, tzinfo=TZ)
    accumulator = HourAccumulator(hour_start=start)

    for minute in range(0, 31, 5):
        accumulator.add_sample(start + timedelta(minutes=minute), 6.0)

    assert accumulator.energy_kwh == 3.0  # 6 kW for half an hour


def test_accumulator_reports_the_remaining_budget():
    start = datetime(2026, 1, 12, 8, 0, tzinfo=TZ)
    accumulator = HourAccumulator(hour_start=start)
    for minute in range(0, 31, 5):
        accumulator.add_sample(start + timedelta(minutes=minute), 6.0)

    now = start + timedelta(minutes=30)
    # 3 kWh already used against a 4 kWh budget leaves 1 kWh for half an hour.
    assert accumulator.allowed_kw(now, limit_kw=4.0) == 2.0


def test_accumulator_reports_zero_when_the_budget_is_spent():
    start = datetime(2026, 1, 12, 8, 0, tzinfo=TZ)
    accumulator = HourAccumulator(hour_start=start)
    for minute in range(0, 31, 5):
        accumulator.add_sample(start + timedelta(minutes=minute), 12.0)

    assert accumulator.allowed_kw(start + timedelta(minutes=30), limit_kw=4.0) == 0.0


def test_accumulator_stops_constraining_at_the_end_of_the_hour():
    start = datetime(2026, 1, 12, 8, 0, tzinfo=TZ)
    accumulator = HourAccumulator(hour_start=start)
    accumulator.add_sample(start, 3.0)

    assert accumulator.allowed_kw(start + timedelta(minutes=59, seconds=59), 4.0) == float("inf")


def test_accumulator_resets_on_the_hour_boundary():
    start = datetime(2026, 1, 12, 8, 0, tzinfo=TZ)
    accumulator = HourAccumulator(hour_start=start)
    accumulator.add_sample(start, 5.0)
    accumulator.add_sample(start + timedelta(minutes=30), 5.0)
    assert accumulator.energy_kwh == 2.5

    accumulator.add_sample(start + timedelta(hours=1, minutes=5), 5.0)
    assert accumulator.hour_start == start + timedelta(hours=1)
    assert accumulator.energy_kwh == 0.0


def test_projection_extrapolates_the_rest_of_the_hour():
    start = datetime(2026, 1, 12, 8, 0, tzinfo=TZ)
    accumulator = HourAccumulator(hour_start=start)
    for minute in range(0, 16, 5):
        accumulator.add_sample(start + timedelta(minutes=minute), 4.0)

    now = start + timedelta(minutes=15)
    assert accumulator.projected_hour_kw(now, assumed_kw=4.0) == 4.0
    assert accumulator.projected_hour_kw(now, assumed_kw=0.0) == 1.0
