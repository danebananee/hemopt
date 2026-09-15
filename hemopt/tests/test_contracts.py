from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hemopt.config import EnergyPriceConfig, PeakWindow
from hemopt.contracts import LoadSample, annualise, compare, settle

TZ = ZoneInfo("Europe/Stockholm")
START = datetime(2026, 1, 12, 0, tzinfo=TZ)

# Single-rate grid, no adders and no VAT, so the tests measure the settlement
# rule itself rather than the tax code on top of it.
BARE = EnergyPriceConfig(
    vat_rate=0.0,
    supplier_markup_ore=0.0,
    certificate_ore=0.0,
    balancing_ore=0.0,
    energy_tax_ore=0.0,
    transfer_fee_high_ore=0.0,
    transfer_fee_normal_ore=0.0,
)
WINDOW = PeakWindow()


def day_of(load_kwh: list[float], spot: list[float], start: datetime = START):
    """One hourly sample per entry, starting at `start`."""
    load = [LoadSample(start + timedelta(hours=i), kwh) for i, kwh in enumerate(load_kwh)]
    return load, spot


def test_hourly_bills_each_hour_at_its_own_price():
    load, spot = day_of([1.0, 1.0], [2.0, 4.0])

    cost = settle(load, spot, "hourly", BARE, WINDOW)

    assert cost.energy_sek == pytest.approx(6.0)


def test_monthly_bills_everything_at_the_month_mean():
    load, spot = day_of([1.0, 1.0], [2.0, 4.0])

    cost = settle(load, spot, "monthly", BARE, WINDOW)

    assert cost.energy_sek == pytest.approx(6.0)


def test_shifting_load_to_the_cheap_hour_only_pays_off_hourly():
    """The whole case for switching contract, in one test."""
    spot = [1.0, 5.0]
    flat, _ = day_of([1.0, 1.0], spot)
    shifted, _ = day_of([2.0, 0.0], spot)

    hourly_gain = (
        settle(flat, spot, "hourly", BARE, WINDOW).energy_sek
        - settle(shifted, spot, "hourly", BARE, WINDOW).energy_sek
    )
    monthly_gain = (
        settle(flat, spot, "monthly", BARE, WINDOW).energy_sek
        - settle(shifted, spot, "monthly", BARE, WINDOW).energy_sek
    )

    assert hourly_gain == pytest.approx(4.0)
    assert monthly_gain == pytest.approx(0.0)


def test_daily_averages_within_a_day_but_not_across_days():
    spot = [1.0, 5.0, 10.0, 10.0]
    load = [
        LoadSample(START, 1.0),
        LoadSample(START + timedelta(hours=1), 1.0),
        LoadSample(START + timedelta(days=1), 1.0),
        LoadSample(START + timedelta(days=1, hours=1), 0.0),
    ]

    cost = settle(load, spot, "daily", BARE, WINDOW)

    # Day one: 2 kWh at the mean of 1 and 5. Day two: 1 kWh at 10.
    assert cost.energy_sek == pytest.approx(2 * 3.0 + 1 * 10.0)


def test_quarterly_resolves_inside_the_hour():
    load = [LoadSample(START + timedelta(minutes=15 * i), 1.0) for i in range(4)]
    spot = [1.0, 2.0, 3.0, 4.0]

    quarterly = settle(load, spot, "quarterly", BARE, WINDOW)
    hourly = settle(load, spot, "hourly", BARE, WINDOW)

    assert quarterly.energy_sek == pytest.approx(10.0)
    # All four quarters fall in the same clock hour, so hourly averages them.
    assert hourly.energy_sek == pytest.approx(10.0)


def test_quarterly_beats_hourly_when_load_hides_in_a_cheap_quarter():
    load = [
        LoadSample(START + timedelta(minutes=15 * i), kwh)
        for i, kwh in enumerate([4.0, 0.0, 0.0, 0.0])
    ]
    spot = [1.0, 5.0, 5.0, 5.0]

    quarterly = settle(load, spot, "quarterly", BARE, WINDOW)
    hourly = settle(load, spot, "hourly", BARE, WINDOW)

    assert quarterly.energy_sek == pytest.approx(4.0)
    assert hourly.energy_sek == pytest.approx(16.0)


def test_fixed_price_ignores_spot_entirely():
    load, spot = day_of([1.0, 1.0], [0.0, 100.0])
    pricing = BARE.model_copy(update={"fixed_price_ore": 80.0})

    cost = settle(load, spot, "fixed", pricing, WINDOW)

    assert cost.energy_sek == pytest.approx(1.6)


def test_vat_and_adders_are_applied_on_top():
    load, spot = day_of([2.0], [1.0])
    pricing = EnergyPriceConfig(
        vat_rate=0.25,
        supplier_markup_ore=7.0,
        certificate_ore=1.4,
        balancing_ore=5.76,
        energy_tax_ore=36.0,
        transfer_fee_high_ore=35.6,
        transfer_fee_normal_ore=35.6,
    )

    cost = settle(load, spot, "hourly", pricing, WINDOW)

    assert cost.energy_sek == pytest.approx(2.0 * 1.0 * 1.25)
    # (7 + 1.4 + 5.76 + 36 + 35.6) ore, with VAT, for two kWh.
    assert cost.adders_sek == pytest.approx(2.0 * 0.8576 * 1.25)


def test_standing_charges_are_prorated_over_the_measured_span():
    load = [LoadSample(START + timedelta(days=i), 1.0) for i in range(10)]
    pricing = BARE.model_copy(
        update={"grid_subscription_sek_per_year": 3650.0, "supplier_fee_sek_per_year": 0.0}
    )

    cost = settle(load, [0.0] * 10, "hourly", pricing, WINDOW)

    assert cost.fixed_sek == pytest.approx(100.0)


def test_single_sample_has_no_standing_charge_to_prorate():
    cost = settle([LoadSample(START, 1.0)], [1.0], "hourly", BARE, WINDOW)

    assert cost.fixed_sek == pytest.approx(0.0)


def test_compare_orders_cheapest_first_and_covers_every_type():
    """Load parked in the cheap hour is cheapest per hour, dearest on average."""
    load = [LoadSample(START + timedelta(hours=i), kwh) for i, kwh in enumerate([3.0, 0.0])]
    spot = [1.0, 9.0]

    results = compare(load, spot, BARE, WINDOW)
    by_contract = {r.contract: r.total_sek for r in results}

    assert set(by_contract) == {"monthly", "daily", "hourly", "quarterly"}
    assert results == sorted(results, key=lambda r: r.total_sek)
    assert results[0].total_sek == pytest.approx(3.0)
    assert by_contract["monthly"] == pytest.approx(15.0)
    # Hourly and quarterly cannot be told apart from hourly measurements.
    assert by_contract["hourly"] == pytest.approx(by_contract["quarterly"])


def test_mismatched_series_are_rejected():
    with pytest.raises(ValueError, match="same length"):
        settle([LoadSample(START, 1.0)], [1.0, 2.0], "hourly", BARE, WINDOW)


def test_annualise_scales_a_measured_span_to_a_year():
    load = [LoadSample(START + timedelta(days=i), 1.0) for i in range(73)]

    assert annualise(100.0, load) == pytest.approx(500.0)


def test_mean_price_is_reported_per_kwh():
    load, spot = day_of([4.0], [2.0])

    cost = settle(load, spot, "hourly", BARE, WINDOW)

    assert cost.mean_ore_per_kwh == pytest.approx(200.0)
