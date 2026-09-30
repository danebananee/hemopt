"""A new house is found and set up from Home Assistant, not from YAML."""

from __future__ import annotations

import pytest

from hemopt.config import Config
from hemopt.discovery import discover, room_key
from hemopt.onboarding import apply_setup, checklist, is_complete


def row(entity_id, state, **attributes):
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


ROWS = [
    row(
        "climate.f7_d4_23_14_49_da_thermostat",
        "heat",
        friendly_name="Badrum Thermostat",
        temperature=22.0,
    ),
    row(
        "sensor.f7_d4_23_14_49_da_temperature",
        "22.4",
        device_class="temperature",
        unit_of_measurement="°C",
    ),
    row(
        "sensor.f7_d4_23_14_49_da_humidity", "48", device_class="humidity", unit_of_measurement="%"
    ),
    row("climate.h66_hpext_control_port_1", "off", friendly_name="EXT 1"),
    row(
        "sensor.h66_hpoutdoor",
        "-3.1",
        device_class="temperature",
        unit_of_measurement="°C",
        friendly_name="Utetemperatur",
    ),
    row("sensor.h66_hppower_consumption", "1450", device_class="power", unit_of_measurement="W"),
    row(
        "sensor.h66_hpwarm_water_1_top", "51", device_class="temperature", unit_of_measurement="°C"
    ),
    row("number.h66_hpwarm_water_1", "52", friendly_name="Varmvatten börvärde"),
    row("sensor.p1_meter_active_power", "2300", device_class="power", unit_of_measurement="W"),
    row(
        "sensor.p1_meter_active_current_l1", "4.1", unit_of_measurement="A", device_class="current"
    ),
    row(
        "sensor.p1_meter_active_current_l2", "9.8", unit_of_measurement="A", device_class="current"
    ),
    row(
        "sensor.p1_meter_active_current_l3", "2.0", unit_of_measurement="A", device_class="current"
    ),
    row("weather.forecast_home", "cloudy"),
    row("sensor.volvo_ex30_battery", "64", unit_of_measurement="%", device_class="battery"),
    row("binary_sensor.easee_cable_connected", "on"),
    row("number.easee_dynamic_charger_limit", "16", unit_of_measurement="A"),
    row("sensor.iphone_battery_level", "80", unit_of_measurement="%", device_class="battery"),
    row("sensor.husbatteri_soc", "55", unit_of_measurement="%", device_class="battery"),
    row("number.husbatteri_charge_power", "0", unit_of_measurement="W"),
]


def first(found, role):
    candidates = found.roles.get(role) or []
    return candidates[0].entity_id if candidates else None


def test_rooms_are_found_with_their_own_sensors():
    found = discover(ROWS, {"latitude": 59.3, "longitude": 18.0})
    assert len(found.rooms) == 1, "the heat pump's EXT port is not a room"
    room = found.rooms[0]
    assert room.name == "Badrum"
    assert room.temperature_entity == "sensor.f7_d4_23_14_49_da_temperature"
    assert room.humidity_entity == "sensor.f7_d4_23_14_49_da_humidity"


def test_each_role_gets_the_obvious_entity_first():
    found = discover(ROWS)
    assert first(found, "outdoor") == "sensor.h66_hpoutdoor"
    assert first(found, "heat_pump_power") == "sensor.h66_hppower_consumption"
    assert first(found, "hot_water_temperature") == "sensor.h66_hpwarm_water_1_top"
    assert first(found, "meter") == "sensor.p1_meter_active_power"
    assert first(found, "weather") == "weather.forecast_home"
    assert first(found, "ev_soc") == "sensor.volvo_ex30_battery"
    assert first(found, "ev_plugged") == "binary_sensor.easee_cable_connected"
    assert first(found, "ev_charger_control") == "number.easee_dynamic_charger_limit"
    assert first(found, "battery_soc") == "sensor.husbatteri_soc"
    assert first(found, "battery_control") == "number.husbatteri_charge_power"
    assert found.phase_currents == [
        "sensor.p1_meter_active_current_l1",
        "sensor.p1_meter_active_current_l2",
        "sensor.p1_meter_active_current_l3",
    ]


def test_a_phone_is_not_mistaken_for_a_battery():
    found = discover(ROWS)
    ids = [c.entity_id for c in found.roles["battery_soc"] + found.roles["ev_soc"]]
    assert "sensor.iphone_battery_level" not in ids


def test_room_keys_are_readable_and_unique():
    taken: set[str] = set()
    assert room_key("Rios rum", taken) == "rios_rum"
    assert room_key("Entré", taken) == "entre"
    assert room_key("Rios rum", taken) == "rios_rum_2"


def test_saving_the_guide_builds_a_valid_house():
    config = apply_setup(
        Config(),
        {
            "site": {"price_area": "SE4", "main_fuse_amps": 20, "weather_entity": "weather.home"},
            "energy_price": {"contract": "quarterly"},
            "heat_pump": {"outdoor_entity": "sensor.ute", "power_entity": ""},
            "rooms": [
                {"name": "Vardagsrum", "temperature_entity": "sensor.vr", "priority": 1},
                {"name": "Kök", "temperature_entity": "sensor.kok", "floor": "Bottenvåning"},
                {"name": "Utan givare"},
            ],
            "ev": {"enabled": True, "departure_time": "06:30", "target_soc_pct": 70},
            "not_a_section": {"x": 1},
        },
    )
    assert [room.key for room in config.rooms] == ["vardagsrum", "kok"]
    assert config.heat_pump.power_entity is None, "a blank field clears the setting"
    assert config.site.price_area == "SE4"
    assert config.ev.enabled and config.ev.departure() == (6, 30)
    assert "site.price_area" in config.panel_managed
    assert is_complete(config)
    assert all(item["done"] for item in checklist(config) if item["key"] in {"rooms", "basics"})


def test_saving_keeps_what_the_guide_does_not_touch():
    before = Config.model_validate(
        {
            "rooms": [
                {"key": "sovrum", "name": "Sovrum", "temperature_entity": "s.a", "heat_share": 1.8}
            ],
            "peak_tariff": {"n_peaks": 3},
        }
    )
    after = apply_setup(
        before, {"rooms": [{"key": "sovrum", "name": "Sovrum", "temperature_entity": "s.b"}]}
    )
    assert after.rooms[0].heat_share == pytest.approx(1.8)
    assert after.rooms[0].temperature_entity == "s.b"
    assert after.peak_tariff.n_peaks == 3


def test_invalid_settings_are_refused_whole():
    with pytest.raises(ValueError):
        apply_setup(Config(), {"site": {"price_area": "SE9"}})


def test_the_engine_adopts_a_new_setup_without_restart(tmp_path, monkeypatch):
    from hemopt.demo import DemoEngine, demo_config

    monkeypatch.setenv("HEMOPT_DATA", str(tmp_path))
    engine = DemoEngine(demo_config(database_path=str(tmp_path / "e.db")))
    before = set(engine.models)
    config = apply_setup(
        engine.config,
        {
            "rooms": [
                {
                    "key": "salong",
                    "name": "Salong",
                    "temperature_entity": "sensor.demo_salong_temperature",
                },
                {"name": "Gäststuga", "temperature_entity": "sensor.gast", "floor": "Övervåning"},
            ]
        },
    )
    engine.reconfigure(config)
    assert set(engine.models) == {"salong", "gaststuga"}
    assert engine.models["salong"] is not None and "salong" in before
    assert engine.models["gaststuga"].tau_slab_hours < 1.0, "an upper floor gets the light prior"
    assert (tmp_path / "profile.json").exists()
