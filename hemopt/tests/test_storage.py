from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hemopt.hotwater import UsageProfile
from hemopt.storage import Store
from hemopt.thermal import ThermalModel


@pytest.fixture
def store(tmp_path) -> Store:
    instance = Store(tmp_path / "test.db")
    yield instance
    instance.close()


def test_samples_round_trip(store: Store):
    now = datetime(2026, 1, 12, 8, tzinfo=UTC)
    store.record_samples([("sensor.a", now, 21.5), ("sensor.a", now + timedelta(minutes=1), 21.6)])

    samples = store.samples("sensor.a", since=now - timedelta(hours=1))
    assert [value for _, value in samples] == [21.5, 21.6]


def test_samples_are_deduplicated_by_timestamp(store: Store):
    now = datetime(2026, 1, 12, 8, tzinfo=UTC)
    store.record_samples([("sensor.a", now, 1.0)])
    store.record_samples([("sensor.a", now, 2.0)])

    assert store.samples("sensor.a", since=now - timedelta(hours=1)) == [(now, 2.0)]


def test_pruning_drops_old_samples(store: Store):
    old = datetime(2026, 1, 1, tzinfo=UTC)
    new = datetime(2026, 3, 1, tzinfo=UTC)
    store.record_samples([("sensor.a", old, 1.0), ("sensor.a", new, 2.0)])

    store.prune_samples(older_than=datetime(2026, 2, 1, tzinfo=UTC))
    assert [value for _, value in store.samples("sensor.a", since=old)] == [2.0]


def test_thermal_model_round_trip(store: Store):
    model = ThermalModel(88.5, 0.42, 0.01, 0.91, 1500, True)
    store.save_thermal_model("kontor", model)

    assert store.thermal_model("kontor") == model
    assert store.thermal_model("saknas") is None


def test_hot_water_profile_round_trip(store: Store):
    profile = UsageProfile(grid={(0, 7): 1.5, (0, 8): 0.2}, observations=42, fitted=True)
    store.save_hot_water_profile(profile)

    restored = store.hot_water_profile()
    assert restored.grid[(0, 7)] == 1.5
    assert restored.observations == 42


def test_hourly_power_is_grouped_by_month(store: Store):
    january = datetime(2026, 1, 12, 8, tzinfo=UTC)
    february = datetime(2026, 2, 12, 8, tzinfo=UTC)
    store.record_hourly_power(january, 4.2)
    store.record_hourly_power(february, 5.1)

    assert store.hourly_power("2026-01") == {january: 4.2}
    assert store.hourly_power("2026-02") == {february: 5.1}


def test_expected_peak_ignores_the_month_being_asked_about():
    """The running month's own threshold must not seed its own estimate."""
    store = Store(":memory:")
    store.record_month_result("2026-01", average_kw=1.0, threshold_kw=0.5, cost_sek=10.0)

    assert store.expected_peak_kw("2026-01", fallback=4.0) == 4.0
    store.close()


def test_expected_peak_prefers_the_same_month_last_year():
    store = Store(":memory:")
    store.record_month_result("2025-01", average_kw=6.0, threshold_kw=5.5, cost_sek=400.0)
    store.record_month_result("2025-11", average_kw=4.0, threshold_kw=3.5, cost_sek=250.0)

    assert store.expected_peak_kw("2026-01", fallback=9.9) == 5.5
    store.close()


def test_expected_peak_falls_back_to_the_latest_measured_month():
    store = Store(":memory:")
    store.record_month_result("2025-11", average_kw=4.0, threshold_kw=3.5, cost_sek=250.0)

    assert store.expected_peak_kw("2026-02", fallback=9.9) == 3.5
    store.close()


def test_settings_round_trip(store: Store):
    store.set_setting("control_enabled", True)
    store.set_setting("priority_kontor", 4)

    assert store.setting("control_enabled") is True
    assert store.setting("priority_kontor") == 4
    assert store.setting("missing", default="x") == "x"


def test_plans_are_capped(store: Store):
    start = datetime(2026, 1, 12, tzinfo=UTC)
    for index in range(210):
        store.save_plan(start + timedelta(minutes=index), {"index": index})

    assert store.latest_plan() == {"index": 209}
