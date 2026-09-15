from __future__ import annotations

import httpx
import pytest

from hemopt.config import Config
from hemopt.doctor import FAIL, OK, WARN, format_report, run_doctor

STATES = {
    "weather.forecast_home": "cloudy",
    "sensor.p1_meter_active_power": "914",
    "sensor.house_total_power": "2400",
    "sensor.h66_hpoutdoor": "-4.2",
    "sensor.h66_hppower_consumption": "1840",
    "sensor.h66_hpwarm_water_1_top": "51.0",
    "sensor.f7_d4_23_14_49_da_temperature": "22.1",
    "climate.f7_d4_23_14_49_da": "heat",
    "sensor.d0_f5_31_05_49_9d_temperature": "20.4",
    "climate.d0_f5_31_05_49_9d": "heat",
}


def make_config(**overrides) -> Config:
    base = {
        "home_assistant": {"base_url": "http://ha", "token": "t"},
        "site": {"weather_entity": "weather.forecast_home"},
        "heat_pump": {
            "outdoor_entity": "sensor.h66_hpoutdoor",
            "power_entity": "sensor.h66_hppower_consumption",
        },
        "hot_water": {"top_temperature_entity": "sensor.h66_hpwarm_water_1_top"},
        "base_load": {"total_power_entity": "sensor.house_total_power"},
        "rooms": [
            {
                "key": "badrum",
                "name": "Badrum",
                "temperature_entity": "sensor.f7_d4_23_14_49_da_temperature",
                "climate_entity": "climate.f7_d4_23_14_49_da",
            }
        ],
    }
    return Config.model_validate(base | overrides)


class FakeHomeAssistant:
    """Stubbed /api/states endpoint whose contents tests can mutate."""

    def __init__(self) -> None:
        self.states = dict(STATES)
        self.offline = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("connection refused")
        if request.url.path == "/api/states":
            return httpx.Response(
                200,
                json=[{"entity_id": key, "state": value} for key, value in self.states.items()],
            )
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def fake_home_assistant(monkeypatch):
    fake = FakeHomeAssistant()
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handle)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
    return fake


def levels(report, label_fragment: str) -> list[str]:
    return [f.level for f in report.findings if label_fragment in f.label]


async def test_a_healthy_config_reports_no_failures():
    report = await run_doctor(make_config())

    assert report.failures == 0
    assert "Allt ser bra ut" in format_report(report)


async def test_a_missing_entity_is_a_failure_with_a_suggestion():
    config = make_config(
        rooms=[
            {
                "key": "badrum",
                "name": "Badrum",
                "temperature_entity": "sensor.f7_d4_23_14_49_db_temperature",
            }
        ]
    )
    report = await run_doctor(config)

    finding = next(f for f in report.findings if "Badrum, temperatur" in f.label)
    assert finding.level == FAIL
    assert "sensor.f7_d4_23_14_49_da_temperature" in finding.suggestions


def test_suggestions_stay_within_the_entity_domain():
    """A wrong climate id should never be answered with a sensor id."""
    from hemopt.doctor import _suggest

    known = ["sensor.f7_d4_23_14_49_da_temperature", "climate.f7_d4_23_14_49_da"]
    assert _suggest("climate.f7_d4_23_14_49_da_thermostat", known) == ["climate.f7_d4_23_14_49_da"]


async def test_a_missing_thermostat_is_only_a_warning():
    """Rooms are still readable and plannable without a controllable thermostat."""
    config = make_config(
        rooms=[
            {
                "key": "badrum",
                "name": "Badrum",
                "temperature_entity": "sensor.f7_d4_23_14_49_da_temperature",
                "climate_entity": "climate.finns_inte",
            }
        ]
    )
    report = await run_doctor(config)

    assert report.failures == 0
    assert levels(report, "Badrum, termostat") == [WARN]


async def test_an_unavailable_entity_is_a_warning(fake_home_assistant):
    fake_home_assistant.states["sensor.h66_hpoutdoor"] = "unavailable"
    report = await run_doctor(make_config())

    assert levels(report, "Utetemperatur") == [WARN]


async def test_a_non_numeric_reading_is_a_warning(fake_home_assistant):
    fake_home_assistant.states["sensor.h66_hpwarm_water_1_top"] = "varmt"
    report = await run_doctor(make_config())

    assert levels(report, "Varmvatten") == [WARN]


async def test_peak_tariff_without_a_house_meter_warns():
    config = make_config(peak_tariff={"enabled": True}, base_load={})
    report = await run_doctor(config)

    assert levels(report, "Effektavgift") == [WARN]
    assert levels(report, "Husets totala effekt") == [WARN]


async def test_ext_entities_are_only_checked_when_ext_is_enabled():
    off = await run_doctor(make_config())
    assert levels(off, "EXT") == []

    on = await run_doctor(
        make_config(
            ext_control={"enabled": True, "block_heating_entity": "climate.f7_d4_23_14_49_da"}
        )
    )
    assert levels(on, "EXT, blockera varme") == [OK]


async def test_an_unreachable_home_assistant_fails_fast(fake_home_assistant):
    fake_home_assistant.offline = True

    report = await run_doctor(make_config())
    assert report.failures == 1
    assert "Home Assistant" in report.findings[0].label


async def test_a_config_without_rooms_fails():
    report = await run_doctor(make_config(rooms=[]))

    assert levels(report, "Rum") == [FAIL]
