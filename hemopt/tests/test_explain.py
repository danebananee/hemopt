from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hemopt.explain import explain_plan, headline
from hemopt.optimizer import HotWaterPlan, Plan, RoomPlan

TZ = ZoneInfo("Europe/Stockholm")


def _plan(*, charge_now: float = 0.0, rising: bool = False) -> Plan:
    start = datetime(2026, 1, 10, 12, tzinfo=TZ)
    times = [start + timedelta(minutes=15 * i) for i in range(24)]
    price = [0.5] * 24
    if rising:
        for i in range(8, 24):
            price[i] = 1.4
    charge = [0.0] * 24
    charge[0] = charge_now
    return Plan(
        start=start,
        times=times,
        step_minutes=15,
        rooms=[
            RoomPlan(
                key="vardagsrum",
                name="Vardagsrum",
                priority=1,
                temperature=[21.5] * 24,
                setpoint=[22.4] * 24,
                heat_fraction=[0.8] * 24,
                comfort_min=21.0,
                comfort_max=22.5,
            )
        ],
        hot_water=HotWaterPlan(
            temperature=[50.0] * 24,
            charge_fraction=charge,
            expected_draw_kwh=[0.1] * 24,
        ),
        heat_pump_kw=[2.0] * 24,
        total_power_kw=[2.5] * 24,
        price_sek_per_kwh=price,
        outdoor_c=[-2.0] * 24,
        hour_peaks=[],
        energy_cost_sek=10.0,
        peak_cost_sek=0.0,
        comfort_penalty_sek=0.0,
    )


def test_explains_dhw_precharge_before_expensive_period():
    actions = explain_plan(_plan(charge_now=0.6, rising=True), 0, control_enabled=True)
    keys = {a.key for a in actions}
    assert "dhw_precharge" in keys
    assert "forvarmer" in headline(actions).lower() or "varmvatten" in headline(actions).lower()


def test_explains_observe_mode_when_control_off():
    actions = explain_plan(_plan(), 0, control_enabled=False)
    assert any(a.key == "observe" for a in actions)
