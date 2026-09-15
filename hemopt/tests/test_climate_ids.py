"""Tests for climate entity id resolution (…_thermostat vs bare MAC)."""

from __future__ import annotations

from types import SimpleNamespace

from hemopt.climate_ids import (
    climate_entity_candidates,
    repair_room_climate_entities,
    resolve_climate_entity,
)


def test_candidates_prefer_thermostat_suffix_from_bare_id():
    assert climate_entity_candidates("climate.d5_ba_fd_c1_0c_c1") == [
        "climate.d5_ba_fd_c1_0c_c1",
        "climate.d5_ba_fd_c1_0c_c1_thermostat",
    ]


def test_candidates_from_temperature_sensor():
    assert climate_entity_candidates(None, "sensor.d5_ba_fd_c1_0c_c1_temperature") == [
        "climate.d5_ba_fd_c1_0c_c1_thermostat",
        "climate.d5_ba_fd_c1_0c_c1",
    ]


def test_resolve_picks_thermostat_when_bare_missing():
    states = {"climate.d5_ba_fd_c1_0c_c1_thermostat": "heat"}
    assert (
        resolve_climate_entity("climate.d5_ba_fd_c1_0c_c1", states)
        == "climate.d5_ba_fd_c1_0c_c1_thermostat"
    )


def test_resolve_from_temperature_when_configured_wrong():
    states = {"climate.e0_ec_2c_c8_5e_2c_thermostat": "heat"}
    assert (
        resolve_climate_entity(
            "climate.fel_id",
            states,
            temperature_entity="sensor.e0_ec_2c_c8_5e_2c_temperature",
        )
        == "climate.e0_ec_2c_c8_5e_2c_thermostat"
    )


def test_repair_rewrites_room_config():
    rooms = [
        SimpleNamespace(
            key="garderob",
            climate_entity="climate.d5_ba_fd_c1_0c_c1",
            temperature_entity="sensor.d5_ba_fd_c1_0c_c1_temperature",
        )
    ]
    states = {"climate.d5_ba_fd_c1_0c_c1_thermostat": "heat"}
    repaired = repair_room_climate_entities(rooms, states)
    assert repaired == [
        {
            "key": "garderob",
            "from": "climate.d5_ba_fd_c1_0c_c1",
            "to": "climate.d5_ba_fd_c1_0c_c1_thermostat",
        }
    ]
    assert rooms[0].climate_entity == "climate.d5_ba_fd_c1_0c_c1_thermostat"
