"""Room settings that must never reach the optimiser in a broken state."""

from __future__ import annotations

import pytest

from hemopt.config import RoomConfig, clamp_priority


def room(**kwargs) -> RoomConfig:
    return RoomConfig(key="r", name="R", temperature_entity="sensor.r", **kwargs)


@pytest.mark.parametrize(
    ("sent", "stored"), [(0, 1), (1, 1), (2, 2), (3, 3), (5, 3), ("2.0", 2), ("x", 1)]
)
def test_priorities_are_clamped_to_the_scale(sent, stored):
    assert clamp_priority(sent) == stored


def test_an_out_of_range_priority_cannot_crash_the_weights():
    config = room()
    config.priority = 5  # assignment is not validated, e.g. from an MQTT slider
    assert config.comfort_weight == room(priority=3).comfort_weight


@pytest.mark.parametrize(
    ("floor", "expected"),
    [
        ("Övervåning", "light"),
        ("Overvaning", "light"),
        ("Bottenvåning", "concrete"),
        ("", "concrete"),
    ],
)
def test_floor_type_is_guessed_from_the_storey(floor, expected):
    assert room(floor=floor).resolved_floor_type == expected


def test_an_explicit_floor_type_wins():
    assert room(floor="Övervåning", floor_type="concrete").resolved_floor_type == "concrete"
