"""End-to-end and unit tests against the simulated house."""

from __future__ import annotations

import json
import threading
import urllib.request

import numpy as np
import pytest

from slingkoll.analysis import Sample, analyse
from slingkoll.design import make_design, phase_count
from slingkoll.discovery import default_settings, discover
from slingkoll.runner import Runner
from slingkoll.server import make_server
from slingkoll.sim import TZ, SimHA, demo_house

START = 1790000000.0
TRUTH = {
    "Vardagsrum": ("ok", "Vardagsrum"),
    "Kök": ("wrong", "Tvättstuga"),
    "Tvättstuga": ("wrong", "Kök"),
    "Sovrum BV": ("wrong", "Hall"),
    "Sovrum 1": ("wrong", "Sovrum 2"),
    "Sovrum 2": ("wrong", "Kontor"),
    "Kontor": ("wrong", "Sovrum 1"),
    "Lekrum": ("ok", "Lekrum"),
}


def test_design_is_balanced_and_separable():
    for loops in (2, 5, 8, 11, 15):
        design = np.array(make_design(loops, seed=1, tries=400))
        assert design.shape == (phase_count(loops), loops)
        assert (design.sum(axis=0) == design.shape[0] // 2).all()
        share = design.mean(axis=1)
        if loops >= 4:
            assert share.min() >= 0.25 and share.max() <= 0.75
        info = np.column_stack([np.ones(design.shape[0]), design])
        assert np.linalg.matrix_rank(info) == loops + 1


def test_discovery_prefers_lk_thermostats_and_skips_their_own_sensors():
    ha = SimHA(demo_house(start=START))
    found = discover(ha.states())
    chosen = [t["entity_id"] for t in found["thermostats"] if t["default"]]
    assert len(chosen) == 8
    assert "climate.h66_hproom_temp_setpoint" not in [t["entity_id"] for t in found["thermostats"]]
    sensors = [s["entity_id"] for s in found["sensors"]]
    assert sensors == ["sensor.hall_temperatur"]
    settings = default_settings(found)
    assert settings.supply_entity == "sensor.h66_hpradiator_forward"
    assert settings.hp_setpoint_entity == "climate.h66_hproom_temp_setpoint"
    assert settings.outdoor_entity == "sensor.h66_hpoutdoor"
    assert settings.hemopt_switch_entity == "switch.hemopt_control_enabled"


def _drive(ha: SimHA, design: list[list[int]], hours: float) -> list[Sample]:
    house = ha.house
    samples = []
    for row in design:
        for thermo, on in zip(house.thermostats, row, strict=True):
            thermo.target = 28.0 if on else 10.0
        for _ in range(int(hours * 60)):
            ha.advance(60)
            temps = {t.entity_id: t.reading for t in house.thermostats}
            temps["sensor.hall_temperatur"] = house.readings.get("sensor.hall_temperatur")
            samples.append(
                Sample(
                    t=house.t,
                    temps=temps,
                    targets={t.entity_id: t.target for t in house.thermostats},
                    cmd={t.entity_id: on for t, on in zip(house.thermostats, row, strict=True)},
                    supply=house.supply,
                    outdoor=house.outdoor(),
                )
            )
    return samples


def test_analysis_finds_every_cross_wiring():
    ha = SimHA(demo_house(start=START))
    thermos = [(t.entity_id, t.name) for t in ha.house.thermostats]
    samples = _drive(ha, make_design(len(thermos), seed=1), 3.0)
    result = analyse(samples, thermos, [("sensor.hall_temperatur", "Hall")], tz=TZ)
    found = {v["name"]: (v["status"], v["heats_name"]) for v in result["thermostats"]}
    assert found == TRUTH
    assert result["settled"]
    kinds = sorted(fix["kind"] for fix in result["fixes"])
    assert kinds == ["move", "move", "move", "move", "swap"]


def test_without_warm_water_nothing_is_claimed():
    ha = SimHA(demo_house(start=START))
    ha.house.hp_setpoint = 21.0
    thermos = [(t.entity_id, t.name) for t in ha.house.thermostats]
    samples = _drive(ha, make_design(len(thermos), seed=1)[:4], 3.0)
    for sample in samples:
        sample.supply = 21.0  # the heat pump never sent any warm water
    result = analyse(samples, thermos, tz=TZ)
    assert result["heat_supply"] == "none"
    assert not any(v["status"] in {"ok", "wrong"} for v in result["thermostats"])
    assert not result["settled"]


@pytest.fixture
def runner(tmp_path):
    ha = SimHA(demo_house(start=START), history_days=2)
    r = Runner(ha, tmp_path)
    r.analyse_every_s = 3 * 3600
    r.background = False
    r.start()
    # Sovrum BV runs cold in the simulated house: its loop heats the hall.
    r.update_settings({"min_temp": 15, "max_temp": 27})
    return r


def _run_until_done(runner: Runner, max_hours: float = 120) -> None:
    for _ in range(int(max_hours * 60)):
        runner.ha.advance(60)
        runner.tick()
        if runner.run["status"] not in {"running", "paused"}:
            return
    raise AssertionError("test never finished")


def test_full_test_restores_everything(runner):
    house = runner.ha.house
    for thermo, target in zip(house.thermostats, [21, 20.5, 21, 22, 20, 21, 21.5, 19], strict=True):
        thermo.target = target
    before = {t.entity_id: t.target for t in house.thermostats}
    runner.update_settings({"extra_sensors": [{"entity_id": "sensor.hall_temperatur", "name": "Hall"}]})
    runner.start_test()
    assert house.hemopt_on is False
    assert {t.target for t in house.thermostats} == {10.0, 28.0}
    _run_until_done(runner)
    assert runner.run["status"] == "finished"
    assert {t.entity_id: t.target for t in house.thermostats} == before
    assert house.hp_setpoint == 21.0
    assert house.hemopt_on is True
    result = runner.run["result"]
    found = {v["name"]: (v["status"], v["heats_name"]) for v in result["thermostats"]}
    assert found == TRUTH


def test_shutdown_restores_and_resumes(runner, tmp_path):
    house = runner.ha.house
    runner.start_test()
    for _ in range(30):
        runner.ha.advance(60)
        runner.tick()
    runner.shutdown()
    assert {t.target for t in house.thermostats} == {21.0}
    assert runner.run["suspended"]

    again = Runner(runner.ha, tmp_path)
    again.start()
    assert again.run["status"] == "running"
    assert len(again.samples) == len(runner.samples)
    runner.ha.advance(60)
    again.tick()
    assert {t.target for t in house.thermostats} == {10.0, 28.0}


def test_long_downtime_abandons_the_test(runner, tmp_path):
    runner.start_test()
    runner.shutdown()
    runner.ha.advance(3 * 3600)
    again = Runner(runner.ha, tmp_path)
    again.start()
    assert again.run["status"] == "stopped"
    assert runner.ha.house.hemopt_on is True


def test_cold_room_pauses_the_test(runner):
    runner.update_settings({"min_temp": 21.5, "max_temp": 26})
    runner.start_test()
    for _ in range(12 * 60):
        runner.ha.advance(60)
        runner.tick()
        if runner.run["status"] == "paused":
            break
    assert runner.run["status"] == "paused"
    assert "för kall" in runner.run["pause"]["reason"]
    assert {t.target for t in runner.ha.house.thermostats} == {21.0}


def test_abort_restores_without_result(runner):
    runner.start_test()
    runner.ha.advance(600)
    runner.tick()
    runner.stop_test(analyse_now=False)
    assert runner.run["status"] == "aborted"
    assert {t.target for t in runner.ha.house.thermostats} == {21.0}


def test_quick_check_flags_symptoms(tmp_path):
    runner = Runner(SimHA(demo_house(start=START), history_days=4), tmp_path)
    runner.start()
    result = runner.quick_check(3)
    flags = {s["name"]: s["flag"] for s in result["symptoms"]}
    assert flags["Kök"] == "warm"
    assert flags["Tvättstuga"] == "cold"
    assert flags["Vardagsrum"] == "normal"


def test_settings_validation(runner):
    with pytest.raises(ValueError):
        runner.update_settings({"min_temp": 24, "max_temp": 25})
    runner.start_test()
    with pytest.raises(ValueError):
        runner.update_settings({"min_temp": 17})


def test_http_api(runner):
    server = make_server(runner, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "static/app.js?v=" in html and "__V__" not in html
        status = json.loads(urllib.request.urlopen(base + "/api/status").read())
        assert len(status["settings"]["thermostats"]) == 8
        request = urllib.request.Request(base + "/api/test/start", data=b"{}", method="POST")
        started = json.loads(urllib.request.urlopen(request).read())
        assert started["run"]["status"] == "running"
        bad = urllib.request.Request(base + "/api/test/start", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(bad)
        assert error.value.code == 400
        assert urllib.request.urlopen(base + "/static/styles.css").status == 200
    finally:
        server.shutdown()


def test_hemopt_house_is_preset_when_its_thermostats_exist():
    from slingkoll.discovery import HEMOPT_HOUSE

    states = [
        {
            "entity_id": entity,
            "state": "heat",
            "attributes": {"friendly_name": "LK", "temperature": 21, "current_temperature": 20.5},
        }
        for entity, _ in HEMOPT_HOUSE
    ] + [
        {"entity_id": "sensor.h66_hpradiator_forward", "state": "31", "attributes": {}},
        {"entity_id": "climate.h66_hproom_temp_setpoint", "state": "heat", "attributes": {"temperature": 21}},
    ]
    settings = default_settings(discover(states))
    assert [t["name"] for t in settings.thermostats][:2] == ["Vardagsrum", "Badrum"]
    assert len(settings.thermostats) == 11
    assert settings.supply_entity == "sensor.h66_hpradiator_forward"
    assert settings.hp_setpoint_entity == "climate.h66_hproom_temp_setpoint"
    assert settings.outdoor_entity is None
    assert settings.floor == "slow"
