"""The savings ledger: two model twins and the bill they would have produced."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_optimizer import build_problem

from hemopt.config import Config, EnergyPriceConfig
from hemopt.optimizer import solve
from hemopt.shadow import (
    LedgerStep,
    TwinOffset,
    next_offset,
    reference_step,
    shifted_problem,
    summarise,
)
from hemopt.storage import Store

TZ = ZoneInfo("Europe/Stockholm")


def test_offsets_survive_a_round_trip_and_reject_garbage():
    offset = TwinOffset(rooms={"salong": (0.4, -0.1)}, tank_c=3.0)
    assert TwinOffset.from_dict(offset.as_dict()) == offset
    assert TwinOffset.from_dict({"rooms": {"x": ["nan", 1]}, "tank_c": "?"}) == TwinOffset()
    assert TwinOffset.from_dict(None) == TwinOffset()


def test_shifting_moves_rooms_and_tank():
    problem = build_problem()
    offset = TwinOffset(rooms={"vardagsrum": (0.5, 0.2)}, tank_c=4.0)
    up = shifted_problem(problem, offset, +1.0)
    down = shifted_problem(problem, offset, -1.0)
    assert up.rooms[0].initial_temperature == pytest.approx(21.5)
    assert down.rooms[0].initial_temperature == pytest.approx(20.5)
    assert up.rooms[1].initial_temperature == pytest.approx(21.0)
    assert up.hot_water.initial_temperature == pytest.approx(56.0)


def test_reference_twin_heats_like_a_thermostat():
    problem = build_problem()
    cold = reference_step(problem, {"vardagsrum": 23.0, "garage": 23.0})
    idle = reference_step(problem, {"vardagsrum": 18.0, "garage": 18.0})
    assert cold.electrical_kwh > idle.electrical_kwh
    assert cold.rooms["vardagsrum"][0] > idle.rooms["vardagsrum"][0]
    # Without a known setpoint the room is held where it is.
    held = reference_step(problem, {})
    assert held.rooms["vardagsrum"][0] == pytest.approx(21.0, abs=0.01)


def test_a_twin_that_matches_reality_has_no_offset():
    problem = build_problem(with_hot_water=False)
    plan = solve(problem)
    reference = reference_step(problem, {})
    fake = replace(
        reference,
        rooms={room.key: (room.temperature[0], room.slab[0]) for room in plan.rooms},
    )
    assert next_offset(plan, fake).rooms == {room.key: (0.0, 0.0) for room in plan.rooms}


def _step(start, spot, spot_day, reference, optimised, house):
    return LedgerStep(
        start=start,
        spot=spot,
        spot_hour=spot,
        spot_day=spot_day,
        adder=0.5,
        reference_kwh=reference,
        optimised_kwh=optimised,
        house_kwh=house,
    )


def test_summary_splits_contract_and_control_effects():
    config = Config(energy_price=EnergyPriceConfig(contract="daily", vat_rate=0.25))
    start = datetime(2026, 1, 12, 0, 0, tzinfo=TZ)
    # Two quarters: one cheap, one expensive, day mean 1.0 SEK.
    steps = [
        _step(start, 0.5, 1.0, reference=1.0, optimised=1.5, house=2.0),
        _step(start + timedelta(minutes=15), 1.5, 1.0, reference=1.0, optimised=0.5, house=1.0),
    ]
    summary = summarise(steps, config, TZ)
    day = summary["days"][0]

    # Current contract: 3 kWh at (1.0 * 1.25 + 0.5).
    assert day["cost_now_sek"] == pytest.approx(3 * 1.75, abs=0.01)
    # Quarterly: 2 kWh at (0.625 + 0.5) + 1 kWh at (1.875 + 0.5).
    assert day["cost_quarter_sek"] == pytest.approx(2 * 1.125 + 2.375, abs=0.01)
    assert day["contract_effect_sek"] == pytest.approx(3 * 1.75 - (2 * 1.125 + 2.375), abs=0.01)
    # Control: +0.5 kWh in the cheap quarter, -0.5 kWh in the dear one.
    assert day["control_effect_sek"] == pytest.approx(-0.5 * 1.125 + 0.5 * 2.375, abs=0.01)
    assert summary["contract"] == "daily"
    assert summary["totals"]["total_sek"] == pytest.approx(day["total_sek"])


def test_quarterly_contract_has_no_contract_effect():
    config = Config(energy_price=EnergyPriceConfig(contract="quarterly"))
    start = datetime(2026, 1, 12, 0, 0, tzinfo=TZ)
    steps = [_step(start, 0.5, 1.0, 1.0, 1.0, 2.0)]
    summary = summarise(steps, config, TZ)
    assert summary["totals"]["contract_effect_sek"] == pytest.approx(0.0)
    assert any("redan kvartspris" in note for note in summary["notes"])


def test_days_without_a_house_meter_fall_back_to_the_heat_pump():
    config = Config(energy_price=EnergyPriceConfig(contract="daily"))
    start = datetime(2026, 1, 12, 0, 0, tzinfo=TZ)
    steps = [_step(start + timedelta(minutes=15 * i), 1.0, 1.0, 0.7, 0.7, None) for i in range(4)]
    summary = summarise(steps, config, TZ)
    assert summary["days"][0]["house_measured"] is False
    assert summary["days"][0]["house_kwh"] == pytest.approx(2.8)


def test_ledger_rows_round_trip_through_the_store(tmp_path):
    store = Store(tmp_path / "ledger.db")
    start = datetime(2026, 1, 12, 8, 0, tzinfo=TZ)
    store.record_house_energy(start, 0.9)
    assert not store.has_shadow_step(start)
    store.record_shadow_step(_step(start, 0.8, 1.0, 0.4, 0.2, None))
    assert store.has_shadow_step(start)
    [row] = store.shadow_steps(start - timedelta(hours=1), TZ)
    # The measured energy booked first is kept when the twins are booked.
    assert row.house_kwh == pytest.approx(0.9)
    assert row.optimised_kwh == pytest.approx(0.2)
    assert row.start == start
