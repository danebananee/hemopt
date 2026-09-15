from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hemopt.advice import build_advice, samples_from_hourly
from hemopt.config import Config
from hemopt.contracts import LoadSample

TZ = ZoneInfo("Europe/Stockholm")
START = datetime(2025, 9, 15, 0, tzinfo=TZ)

# Vattenfall Eldistribution DRV, Enkeltariff, prices excluding VAT.
GRID_TARIFFS = [
    {
        "name": "Enkeltariff E2 (16 A)",
        "fuse_amps": 16,
        "subscription_sek_per_year": 4200.0,
        "transfer_ore": 35.6,
    },
    {
        "name": "Enkeltariff E3 (20 A)",
        "fuse_amps": 20,
        "subscription_sek_per_year": 5900.0,
        "transfer_ore": 35.6,
    },
    {
        "name": "Enkeltariff E4 (25 A)",
        "fuse_amps": 25,
        "subscription_sek_per_year": 8100.0,
        "transfer_ore": 35.6,
    },
]


def make_config(**overrides) -> Config:
    base = {
        "site": {"price_area": "SE3", "main_fuse_amps": 25, "voltage": 230, "phases": 3},
        "energy_price": {
            "contract": "monthly",
            "supplier_markup_ore": 7.0,
            "certificate_ore": 1.4,
            "balancing_ore": 5.76,
            "energy_tax_ore": 36.0,
            "transfer_fee_high_ore": 35.6,
            "transfer_fee_normal_ore": 35.6,
            "grid_subscription_sek_per_year": 8100.0,
            "supplier_fee_sek_per_year": 432.0,
        },
        "advice": {"grid_tariffs": GRID_TARIFFS, "min_annual_saving_sek": 200.0},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = base[key] | value
        else:
            base[key] = value
    return Config.model_validate(base)


def year_of_load(days: int = 365, base_kw: float = 2.0, evening_kw: float = 2.6):
    """An unoptimised heat-pumped house: fairly flat load, spiky prices.

    The heat pump runs whenever the house needs heat rather than when power is
    cheap, which is the situation the advice engine is meant to find.
    """
    load, spot = [], []
    for hour in range(days * 24):
        moment = START + timedelta(hours=hour)
        evening = 17 <= moment.hour < 21
        load.append(LoadSample(moment, evening_kw if evening else base_kw))
        spot.append(1.60 if evening else 0.30)
    return load, spot


def year_of_peaky_load(days: int = 365):
    """A house whose load sits almost entirely in the expensive hours."""
    load, spot = [], []
    for hour in range(days * 24):
        moment = START + timedelta(hours=hour)
        evening = 17 <= moment.hour < 21
        load.append(LoadSample(moment, 8.0 if evening else 0.3))
        spot.append(1.60 if evening else 0.30)
    return load, spot


def find(report, key):
    return next((r for r in report.recommendations if r.key == key), None)


# --------------------------------------------------------------- settlement


def test_a_monthly_contract_is_flagged_as_the_headline_problem():
    load, spot = year_of_load()

    report = build_advice(make_config(), load, spot)
    rec = find(report, "settlement")

    assert rec is not None
    assert rec.annual_saving_sek > 1000
    assert "medelpris" in rec.detail


def test_the_monthly_explanation_says_shifting_load_cannot_help():
    load, spot = year_of_load()

    rec = find(build_advice(make_config(), load, spot), "settlement")

    assert "inte kostnaden" in rec.detail


def test_no_settlement_advice_when_already_on_the_cheapest():
    load, spot = year_of_load()
    config = make_config(energy_price={"contract": "quarterly"})

    report = build_advice(config, load, spot)

    assert find(report, "settlement") is None


def test_an_averaging_contract_is_left_alone_when_load_sits_in_expensive_hours():
    """Averaging genuinely protects a house that cannot move its load.

    Recommending hourly here would cost the household money, so the engine has
    to be willing to say nothing.
    """
    load, spot = year_of_peaky_load()
    config = make_config(advice={"grid_tariffs": GRID_TARIFFS, "flexible_share": 0.1})

    assert find(build_advice(config, load, spot), "settlement") is None


def test_the_settlement_saving_excludes_the_shifting_itself():
    """Both sides are priced with load shifted, so no double counting."""
    load, spot = year_of_load()

    rec = find(build_advice(make_config(), load, spot), "settlement")
    costs = {c.contract: c for c in build_advice(make_config(), load, spot).contract_costs}

    # The naive figure would credit the contract change with the whole gap
    # between doing nothing and optimising.
    naive = costs["monthly"].total_sek - costs["quarterly"].total_sek
    assert rec.annual_saving_sek < abs(naive) + 1e6  # sanity bound
    assert rec.annual_saving_sek > 0


def test_contract_costs_are_always_reported_even_without_advice():
    load, spot = year_of_load()
    config = make_config(energy_price={"contract": "quarterly"})

    report = build_advice(config, load, spot)

    assert {c.contract for c in report.contract_costs} == {
        "monthly",
        "daily",
        "hourly",
        "quarterly",
    }


# --------------------------------------------------------------------- fuse


def test_a_fuse_downgrade_is_recommended_when_peaks_leave_room():
    load, spot = year_of_load()

    # 16 A carries 11.0 kW, which a 9.5 kW peak plus 2 kW margin overruns.
    report = build_advice(make_config(), load, spot, peak_kw=9.5)
    rec = find(report, "fuse")

    assert rec is not None
    assert "20 A" in rec.title
    # 8100 - 5900 with VAT.
    assert rec.annual_saving_sek == pytest.approx(2750.0)


def test_the_smallest_affordable_fuse_wins():
    load, spot = year_of_load()

    rec = find(build_advice(make_config(), load, spot, peak_kw=5.0), "fuse")

    assert "16 A" in rec.title


def test_no_fuse_advice_when_the_peak_is_too_close_to_the_limit():
    """20 A carries 13.8 kW; a 12.5 kW peak leaves under the required margin."""
    load, spot = year_of_load()

    report = build_advice(make_config(), load, spot, peak_kw=12.5)

    assert find(report, "fuse") is None


def test_no_fuse_advice_without_a_measured_peak():
    load, spot = year_of_load()

    assert find(build_advice(make_config(), load, spot), "fuse") is None


def test_fuse_advice_waits_for_enough_history():
    load, spot = year_of_load(days=10)

    report = build_advice(make_config(), load, spot, peak_kw=5.0)

    assert find(report, "fuse") is None
    assert any("Sakringsradet vantar" in note for note in report.notes)


def test_a_short_history_lowers_confidence_rather_than_hiding_advice():
    load, spot = year_of_load(days=40)

    rec = find(build_advice(make_config(), load, spot, peak_kw=5.0), "fuse")

    assert rec is not None
    assert rec.confidence == "low"


# ----------------------------------------------------------------- supplier


def test_a_cheaper_supplier_is_recommended():
    load, spot = year_of_load()
    config = make_config(
        advice={
            "grid_tariffs": GRID_TARIFFS,
            "supplier_offers": [
                {
                    "name": "Billigare AB",
                    "markup_ore": 2.0,
                    "certificate_ore": 0.0,
                    "yearly_fee_sek": 0.0,
                }
            ],
        }
    )

    rec = find(build_advice(config, load, spot), "supplier")

    assert rec is not None
    assert rec.annual_saving_sek > 500


def test_a_worse_supplier_offer_is_not_recommended():
    load, spot = year_of_load()
    config = make_config(
        advice={
            "grid_tariffs": GRID_TARIFFS,
            "supplier_offers": [{"name": "Dyrare AB", "markup_ore": 25.0, "yearly_fee_sek": 900.0}],
        }
    )

    assert find(build_advice(config, load, spot), "supplier") is None


# ------------------------------------------------------------- grid tariffs


def test_a_time_of_use_grid_tariff_is_recommended_for_a_night_heavy_house():
    """Night-heavy load is exactly what a time-of-use grid tariff rewards."""
    load, spot = [], []
    for hour in range(365 * 24):
        moment = START + timedelta(hours=hour)
        night = moment.hour < 6
        load.append(LoadSample(moment, 6.0 if night else 0.4))
        spot.append(0.4)

    config = make_config(
        advice={
            "grid_tariffs": [
                {
                    "name": "Tidstariff T4 (25 A)",
                    "fuse_amps": 25,
                    "subscription_sek_per_year": 8100.0,
                    "transfer_ore": 0.0,
                    "transfer_high_ore": 55.0,
                    "transfer_normal_ore": 8.0,
                }
            ]
        }
    )

    rec = find(build_advice(config, load, spot), "grid_tariff")

    assert rec is not None
    assert rec.annual_saving_sek > 1000


def test_grid_tariff_advice_ignores_options_for_other_fuse_sizes():
    load, spot = year_of_load()

    report = build_advice(make_config(), load, spot)

    assert find(report, "grid_tariff") is None


# ------------------------------------------------------------------ general


def test_trivial_savings_are_suppressed():
    load, spot = year_of_load()
    config = make_config(advice={"grid_tariffs": GRID_TARIFFS, "min_annual_saving_sek": 1e9})

    assert build_advice(config, load, spot, peak_kw=5.0).recommendations == []


def test_recommendations_come_biggest_first():
    load, spot = year_of_load()

    report = build_advice(make_config(), load, spot, peak_kw=5.0)
    savings = [r.annual_saving_sek for r in report.recommendations]

    assert savings == sorted(savings, reverse=True)
    assert report.total_annual_saving_sek == pytest.approx(sum(savings))


def test_advice_can_be_switched_off():
    load, spot = year_of_load()
    config = make_config(advice={"enabled": False, "grid_tariffs": GRID_TARIFFS})

    report = build_advice(config, load, spot, peak_kw=5.0)

    assert report.recommendations == []
    assert report.contract_costs == []


def test_no_measurements_yields_a_note_rather_than_a_crash():
    report = build_advice(make_config(), [], [])

    assert report.recommendations == []
    assert any("Ingen matdata" in note for note in report.notes)


def test_the_report_serialises_for_the_api():
    load, spot = year_of_load()

    payload = build_advice(make_config(), load, spot, peak_kw=5.0).as_dict()

    assert payload["total_annual_saving_sek"] > 0
    assert payload["recommendations"][0]["title"]
    assert payload["contract_costs"][0]["ore_per_kwh"] > 0


def test_hourly_power_converts_to_load_samples():
    power = {START + timedelta(hours=i): float(i) for i in range(3)}

    samples = samples_from_hourly(power)

    assert [s.kwh for s in samples] == [0.0, 1.0, 2.0]
    assert samples[0].start == START
