"""Judging the main fuse from the current on each phase."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hemopt.fuse import PhaseHour, assess, hours_from_samples, judge

TZ = ZoneInfo("Europe/Stockholm")


def month_of_load(peak: float, mean: float, days: int = 40, month: int = 1) -> list[PhaseHour]:
    start = datetime(2026, month, 1, tzinfo=TZ)
    hours = []
    for hour in range(days * 24):
        moment = start + timedelta(hours=hour)
        busy = moment.hour in (7, 17, 18)
        for phase in (1, 2, 3):
            hours.append(
                PhaseHour(
                    moment,
                    phase,
                    max_a=peak if busy and phase == 2 else peak * 0.4,
                    mean_a=mean if busy and phase == 2 else mean * 0.4,
                )
            )
    return hours


def test_levels_follow_how_a_fuse_behaves():
    assert judge(25, peak_a=15, sustained_a=10).level == "comfortable"
    assert judge(20, peak_a=19, sustained_a=14).level == "ok"
    assert judge(16, peak_a=19, sustained_a=14).level == "tight"
    assert judge(16, peak_a=25, sustained_a=17).level == "too_small"


def test_a_big_fuse_on_a_modest_house_can_come_down():
    report = assess(month_of_load(peak=13.0, mean=8.0), current_amps=25)
    assert report.status == "can_downsize"
    assert report.suggested_amps == 16
    assert "16 A" in report.summary
    assert report.winter_covered


def test_a_fuse_close_to_its_limit_is_flagged_with_the_times():
    report = assess(month_of_load(peak=18.0, mean=12.0), current_amps=16)
    assert report.status == "tight"
    assert report.close_calls, "the hours near the limit are listed"
    assert all(call.phase == 2 for call in report.close_calls)


def test_a_fuse_that_is_too_small_says_so():
    report = assess(month_of_load(peak=26.0, mean=18.0), current_amps=16)
    assert report.status == "too_small"
    assert report.suggested_amps is not None and report.suggested_amps > 16


def test_a_few_days_are_not_enough_to_advise_a_smaller_fuse():
    report = assess(month_of_load(peak=10.0, mean=6.0, days=5), current_amps=25)
    assert report.status == "learning"


def test_summer_data_is_called_out():
    report = assess(month_of_load(peak=13.0, mean=8.0, month=7), current_amps=25)
    assert not report.winter_covered
    assert "vinter" in report.detail


def test_no_data_explains_what_is_needed():
    report = assess([], current_amps=20)
    assert report.status == "no_data"
    assert "fasström" in report.detail.lower() or "P1" in report.detail


def test_minute_samples_become_hourly_max_and_mean():
    start = datetime(2026, 1, 5, 17, tzinfo=TZ)
    samples = {
        1: [(start + timedelta(minutes=m), 5.0 + (10.0 if m == 30 else 0.0)) for m in range(60)]
    }
    [hour] = hours_from_samples(samples)
    assert hour.max_a == 15.0
    assert 5.0 < hour.mean_a < 5.5
