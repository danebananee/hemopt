from __future__ import annotations

import json

import pytest

from hemopt.config import Config, profile_path


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Point HEMOPT_DATA at a scratch directory and clear the add-on env."""
    monkeypatch.setenv("HEMOPT_DATA", str(tmp_path))
    for key in list(_ADDON_ENV):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


_ADDON_ENV = (
    "HEMOPT_HA_URL",
    "HEMOPT_HA_TOKEN",
    "HEMOPT_MQTT_HOST",
    "HEMOPT_MQTT_PORT",
    "HEMOPT_MQTT_USERNAME",
    "HEMOPT_MQTT_PASSWORD",
    "HEMOPT_PRICE_AREA",
    "HEMOPT_CONTRACT",
)


def test_an_empty_environment_yields_usable_defaults():
    config = Config.resolve()

    assert config.site.price_area == "SE3"
    assert config.energy_price.contract == "hourly"
    assert config.rooms == []


def test_supervisor_environment_supplies_credentials(monkeypatch):
    monkeypatch.setenv("HEMOPT_HA_URL", "http://supervisor/core")
    monkeypatch.setenv("HEMOPT_HA_TOKEN", "supervisor-token")

    config = Config.resolve()

    assert config.home_assistant.base_url == "http://supervisor/core"
    assert config.home_assistant.token == "supervisor-token"


def test_mqtt_is_enabled_when_the_broker_is_announced(monkeypatch):
    monkeypatch.setenv("HEMOPT_MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("HEMOPT_MQTT_PORT", "1884")
    monkeypatch.setenv("HEMOPT_MQTT_USERNAME", "addons")
    monkeypatch.setenv("HEMOPT_MQTT_PASSWORD", "secret")

    config = Config.resolve()

    assert config.mqtt.enabled is True
    assert (config.mqtt.host, config.mqtt.port) == ("core-mosquitto", 1884)
    assert config.mqtt.username == "addons"


def test_no_broker_leaves_mqtt_alone():
    assert Config.resolve().mqtt.host == Config().mqtt.host


def test_addon_options_select_area_and_contract(monkeypatch):
    monkeypatch.setenv("HEMOPT_PRICE_AREA", "SE2")
    monkeypatch.setenv("HEMOPT_CONTRACT", "quarterly")

    config = Config.resolve()

    assert config.site.price_area == "SE2"
    assert config.energy_price.contract == "quarterly"


def test_the_database_lands_in_the_addon_data_directory(isolated_data):
    assert Config.resolve().database_path == str(isolated_data / "hemopt.db")


def test_a_saved_profile_is_picked_up(isolated_data):
    Config.model_validate(
        {
            "site": {"price_area": "SE4"},
            "rooms": [{"key": "kontor", "name": "Kontor", "temperature_entity": "sensor.k"}],
        }
    ).save_profile()

    config = Config.resolve()

    assert config.site.price_area == "SE4"
    assert [room.key for room in config.rooms] == ["kontor"]


def test_the_environment_overrides_a_saved_profile(monkeypatch):
    Config.model_validate({"site": {"price_area": "SE4"}}).save_profile()
    monkeypatch.setenv("HEMOPT_PRICE_AREA", "SE1")

    assert Config.resolve().site.price_area == "SE1"


def test_an_explicit_config_file_wins_over_the_profile(tmp_path):
    Config.model_validate({"site": {"price_area": "SE4"}}).save_profile()
    path = tmp_path / "config.yaml"
    path.write_text("site:\n  price_area: SE2\n", encoding="utf-8")

    assert Config.resolve(path).site.price_area == "SE2"


def test_saved_profiles_never_contain_credentials(isolated_data):
    """A profile ends up in backups; Supervisor credentials must not."""
    Config.model_validate(
        {
            "home_assistant": {"base_url": "http://ha", "token": "long-lived-secret"},
            "mqtt": {"username": "addons", "password": "broker-secret"},
        }
    ).save_profile()

    saved = json.loads(profile_path().read_text(encoding="utf-8"))

    assert saved["home_assistant"]["token"] == ""
    assert saved["mqtt"]["username"] is None
    assert saved["mqtt"]["password"] is None


def test_a_saved_profile_reloads_credentials_from_the_environment(monkeypatch):
    Config.model_validate(
        {"home_assistant": {"base_url": "http://ha", "token": "gone-after-save"}}
    ).save_profile()
    monkeypatch.setenv("HEMOPT_HA_TOKEN", "fresh-token")

    assert Config.resolve().home_assistant.token == "fresh-token"
