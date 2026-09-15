"""Whole-house power meter discovery."""

from hemopt.meters import pick_default_total_power, suggest_total_power_entities


def test_homewizard_total_beats_phase_sensors():
    states = {
        "sensor.p1_meter_active_power_l1_w": "223",
        "sensor.p1_meter_active_power_l2_w": "629",
        "sensor.p1_meter_active_power_l3_w": "61",
        "sensor.p1_meter_active_power": "914",
        "sensor.p1_meter_energy_import": "35448.94",
    }

    candidates = suggest_total_power_entities(states)
    assert [row["entity_id"] for row in candidates] == ["sensor.p1_meter_active_power"]
    assert pick_default_total_power(states) == "sensor.p1_meter_active_power"


def test_ambiguous_meters_are_not_auto_adopted():
    states = {
        "sensor.p1_meter_active_power": "900",
        "sensor.garage_active_power": "120",
    }
    assert pick_default_total_power(states) is None
    assert len(suggest_total_power_entities(states)) == 2


def test_non_numeric_sensors_are_ignored():
    states = {"sensor.p1_meter_active_power": "unavailable"}
    assert suggest_total_power_entities(states) == []
    assert pick_default_total_power(states) is None
