"""Tests for the temporary loop↔thermostat cross-wiring diagnostic."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from hemopt.loop_mapping import (
    RoomSeries,
    SuggestedSwap,
    analyse_loop_mapping,
    apply_climate_swaps,
    format_report,
)

TZ = ZoneInfo("Europe/Stockholm")


def _simulate_pair(
    *,
    swapped: bool,
    hours: int = 24 * 10,
    step_minutes: int = 15,
) -> tuple[list[RoomSeries], dict[datetime, float]]:
    """Two rooms with independent setpoints; heat either self or the other."""
    dt = step_minutes / 60.0
    moment = datetime(2026, 1, 1, tzinfo=TZ)
    outdoor_c = -5.0
    indoor = {"a": 20.5, "b": 20.5}
    setpoints = {"a": 21.0, "b": 21.0}

    indoor_hist = {"a": {}, "b": {}}
    setpoint_hist = {"a": {}, "b": {}}
    outdoor: dict[datetime, float] = {}

    for index in range(int(hours / dt)):
        # Slow outdoor swing + staggered setpoint pulses for excitation.
        outdoor_now = outdoor_c + 4.0 * ((index // 48) % 2) - 2.0
        if index % 96 < 32:
            setpoints["a"] = 22.0
        elif index % 96 < 48:
            setpoints["a"] = 19.5
        else:
            setpoints["a"] = 21.0

        if (index + 40) % 96 < 32:
            setpoints["b"] = 22.0
        elif (index + 40) % 96 < 48:
            setpoints["b"] = 19.5
        else:
            setpoints["b"] = 21.0

        call_a = max(0.0, min(1.0, (setpoints["a"] - indoor["a"]) / 0.5))
        call_b = max(0.0, min(1.0, (setpoints["b"] - indoor["b"]) / 0.5))

        # Physical loops: either matched or crossed.
        heat_into_a = call_b if swapped else call_a
        heat_into_b = call_a if swapped else call_b

        for key, heat in (("a", heat_into_a), ("b", heat_into_b)):
            drift = (outdoor_now - indoor[key]) / 60.0
            indoor[key] = indoor[key] + dt * (drift + 0.45 * heat)

        outdoor[moment] = outdoor_now
        for key in ("a", "b"):
            indoor_hist[key][moment] = indoor[key]
            setpoint_hist[key][moment] = setpoints[key]

        moment += timedelta(minutes=step_minutes)

    rooms = [
        RoomSeries(
            key="a",
            name="Rum A",
            climate_entity="climate.a",
            indoor=indoor_hist["a"],
            setpoint=setpoint_hist["a"],
        ),
        RoomSeries(
            key="b",
            name="Rum B",
            climate_entity="climate.b",
            indoor=indoor_hist["b"],
            setpoint=setpoint_hist["b"],
        ),
    ]
    return rooms, outdoor


def test_detects_correct_wiring():
    rooms, outdoor = _simulate_pair(swapped=False)
    report = analyse_loop_mapping(rooms, outdoor, history_days=10)

    by_key = {room.room_key: room for room in report.rooms}
    assert by_key["a"].status == "ok"
    assert by_key["b"].status == "ok"
    assert by_key["a"].best_response_key == "a"
    assert by_key["b"].best_response_key == "b"
    assert report.suggested_swaps == []


def test_detects_swapped_loops():
    rooms, outdoor = _simulate_pair(swapped=True)
    report = analyse_loop_mapping(rooms, outdoor, history_days=10)

    by_key = {room.room_key: room for room in report.rooms}
    assert by_key["a"].status == "suspect_swap"
    assert by_key["b"].status == "suspect_swap"
    assert by_key["a"].best_response_key == "b"
    assert by_key["b"].best_response_key == "a"
    assert len(report.suggested_swaps) == 1
    swap = report.suggested_swaps[0]
    assert {swap.room_a, swap.room_b} == {"a", "b"}
    assert swap.confidence >= 0.35


def test_apply_climate_swaps_exchanges_entities():
    rooms = [
        SimpleNamespace(key="a", climate_entity="climate.a"),
        SimpleNamespace(key="b", climate_entity="climate.b"),
    ]
    applied = apply_climate_swaps(
        rooms,
        [SuggestedSwap(room_a="a", room_b="b", confidence=0.9, reason="test")],
    )
    assert applied
    assert rooms[0].climate_entity == "climate.b"
    assert rooms[1].climate_entity == "climate.a"


def test_apply_skips_low_confidence():
    rooms = [
        SimpleNamespace(key="a", climate_entity="climate.a"),
        SimpleNamespace(key="b", climate_entity="climate.b"),
    ]
    applied = apply_climate_swaps(
        rooms,
        [SuggestedSwap(room_a="a", room_b="b", confidence=0.1, reason="weak")],
    )
    assert applied == []
    assert rooms[0].climate_entity == "climate.a"


def test_format_report_mentions_temporary():
    rooms, outdoor = _simulate_pair(swapped=False)
    text = format_report(analyse_loop_mapping(rooms, outdoor, history_days=10))
    assert "tillfällig" in text.lower()


def test_insufficient_rooms_returns_note():
    report = analyse_loop_mapping([], {}, history_days=1)
    assert report.rooms == []
    assert any("minst två" in note for note in report.notes)
