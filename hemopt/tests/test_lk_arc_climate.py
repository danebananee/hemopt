"""Unit tests for LK Arc Climate discovery helpers (no Home Assistant runtime)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "custom_components" / "lk_arc_climate" / "climate.py"


def _load_climate_helpers():
    """Load climate.py without pulling in homeassistant."""
    # Stub the HA imports the module needs at import time.
    ha = type(sys)("homeassistant")
    ha.components = type(sys)("homeassistant.components")
    climate_mod = type(sys)("homeassistant.components.climate")

    class _ClimateEntity:
        pass

    class _Feature:
        TARGET_TEMPERATURE = 1

    class _HVACAction:
        HEATING = "heating"
        IDLE = "idle"

    class _HVACMode:
        HEAT = "heat"

    climate_mod.ClimateEntity = _ClimateEntity
    climate_mod.ClimateEntityFeature = _Feature
    climate_mod.HVACAction = _HVACAction
    climate_mod.HVACMode = _HVACMode
    ha.components.climate = climate_mod

    ha.config_entries = type(sys)("homeassistant.config_entries")
    ha.config_entries.ConfigEntry = object
    ha.const = type(sys)("homeassistant.const")
    ha.const.ATTR_TEMPERATURE = "temperature"
    ha.const.UnitOfTemperature = type("U", (), {"CELSIUS": "°C"})
    ha.core = type(sys)("homeassistant.core")
    ha.core.HomeAssistant = object
    ha.core.callback = lambda f: f
    ha.helpers = type(sys)("homeassistant.helpers")
    ha.helpers.entity = type(sys)("homeassistant.helpers.entity")
    ha.helpers.entity.DeviceInfo = dict
    ha.helpers.entity_platform = type(sys)("homeassistant.helpers.entity_platform")
    ha.helpers.entity_platform.AddEntitiesCallback = object
    ha.helpers.update_coordinator = type(sys)("homeassistant.helpers.update_coordinator")

    class _CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

    ha.helpers.update_coordinator.CoordinatorEntity = _CoordinatorEntity

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.components"] = ha.components
    sys.modules["homeassistant.components.climate"] = climate_mod
    sys.modules["homeassistant.config_entries"] = ha.config_entries
    sys.modules["homeassistant.const"] = ha.const
    sys.modules["homeassistant.core"] = ha.core
    sys.modules["homeassistant.helpers"] = ha.helpers
    sys.modules["homeassistant.helpers.entity"] = ha.helpers.entity
    sys.modules["homeassistant.helpers.entity_platform"] = ha.helpers.entity_platform
    sys.modules["homeassistant.helpers.update_coordinator"] = ha.helpers.update_coordinator

    # Also stub relative const import by loading const first
    const_path = ROOT / "custom_components" / "lk_arc_climate" / "const.py"
    const_spec = importlib.util.spec_from_file_location(
        "custom_components.lk_arc_climate.const", const_path
    )
    const_mod = importlib.util.module_from_spec(const_spec)
    sys.modules["custom_components.lk_arc_climate.const"] = const_mod
    sys.modules["custom_components.lk_arc_climate"] = type(sys)("custom_components.lk_arc_climate")
    const_spec.loader.exec_module(const_mod)

    spec = importlib.util.spec_from_file_location(
        "custom_components.lk_arc_climate.climate", MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["custom_components.lk_arc_climate.climate"] = mod
    # Patch relative import
    mod.__package__ = "custom_components.lk_arc_climate"
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def climate_mod():
    return _load_climate_helpers()


def test_iter_arc_sense_includes_non_tune_roles(climate_mod):
    class Coord:
        data = {
            "devices": [
                {
                    "mac": "c8:1b:04:e0:7e:90",
                    "deviceTitle": {
                        "deviceGroup": "arc",
                        "deviceType": "arc-sense",
                        "deviceRole": "arc-sense",  # NOT arc-tune — stock integration skips
                        "identity": "c8:1b:04:e0:7e:90",
                        "zone": {"zoneName": "Tvättstuga"},
                    },
                    "measurement": {
                        "currentTemperature": 225,
                        "desiredTemperature": 200,
                    },
                }
            ]
        }

    devices = climate_mod._iter_arc_sense(Coord())
    assert len(devices) == 1
    assert devices[0]["mac"] == "c8:1b:04:e0:7e:90"


def test_tenths_to_c(climate_mod):
    assert climate_mod._tenths_to_c(200) == 20.0
    assert climate_mod._tenths_to_c(225) == 22.5
    assert climate_mod._tenths_to_c(None) is None


def test_mac_with_colons(climate_mod):
    assert climate_mod._mac_with_colons("c8:1b:04:e0:7e:90") == "c8:1b:04:e0:7e:90"
    assert climate_mod._mac_with_colons("c8_1b_04_e0_7e_90") == "c8:1b:04:e0:7e:90"
    assert climate_mod._mac_with_colons("C81B04E07E90") == "c8:1b:04:e0:7e:90"


@pytest.mark.asyncio
async def test_post_desired_temperature_verifies_cloud(climate_mod):
    """Write must re-read measurement and reject a fake HTTP 200."""

    class _Resp:
        status = 200

        async def text(self):
            return "{}"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        def post(self, url, json=None, headers=None):
            assert "measurement/true" in url
            assert json["desiredTemperature"] == 215
            return _Resp()

    class _Lk:
        BASE_URL = "https://link2.lk.nu/"
        session = _Session()
        jwt_token = "token"
        device_measurements = {
            "c8:1b:04:e0:7e:90": {
                "currentTemperature": 200,
                "desiredTemperature": 200,
            }
        }

        def _get_headers(self):
            return {"content-type": "application/json"}

        async def get_device_measurement(self, mac, force_update=False):
            # After POST, pretend the cloud accepted the new value.
            if self.device_measurements[mac]["desiredTemperature"] == 215:
                return True
            # First call (before POST) returns current cache.
            return True

    class _Coord:
        data = {
            "devices": [
                {
                    "mac": "c8:1b:04:e0:7e:90",
                    "deviceTitle": {
                        "deviceGroup": "arc",
                        "deviceType": "arc-sense",
                        "identity": "c8:1b:04:e0:7e:90",
                        "zone": {"zoneName": "Tvattstuga"},
                    },
                    "measurement": {
                        "currentTemperature": 200,
                        "desiredTemperature": 200,
                    },
                }
            ]
        }

        def _apply_device_measurement(self, device_id, measurement):
            self.data["devices"][0]["measurement"] = measurement

    entity = climate_mod.LKArcClimate(
        _Coord(),
        _Coord.data["devices"][0],
        mac="c8:1b:04:e0:7e:90",
        identity="c8:1b:04:e0:7e:90",
    )

    lk = _Lk()

    # Simulate POST mutating the cloud, then verification reading it back.
    original_post = lk.session.post

    def post_and_mutate(url, json=None, headers=None):
        lk.device_measurements["c8:1b:04:e0:7e:90"] = dict(json)
        return original_post(url, json=json, headers=headers)

    lk.session.post = post_and_mutate
    assert await entity._post_desired_temperature(lk, "c8:1b:04:e0:7e:90", 21.5) is True


@pytest.mark.asyncio
async def test_post_desired_temperature_rejects_unverified(climate_mod):
    class _Resp:
        status = 200

        async def text(self):
            return "{}"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        def post(self, url, json=None, headers=None):
            return _Resp()

    class _Lk:
        BASE_URL = "https://link2.lk.nu/"
        session = _Session()
        jwt_token = "token"
        device_measurements = {
            "c8:1b:04:e0:7e:90": {
                "currentTemperature": 200,
                "desiredTemperature": 200,  # never changes → fake success
            }
        }

        def _get_headers(self):
            return {}

        async def get_device_measurement(self, mac, force_update=False):
            return True

    class _Coord:
        data = {
            "devices": [
                {
                    "mac": "c8:1b:04:e0:7e:90",
                    "deviceTitle": {
                        "deviceGroup": "arc",
                        "deviceType": "arc-sense",
                        "identity": "c8:1b:04:e0:7e:90",
                        "zone": {"zoneName": "Tvattstuga"},
                    },
                    "measurement": {"desiredTemperature": 200},
                }
            ]
        }

    entity = climate_mod.LKArcClimate(
        _Coord(),
        _Coord.data["devices"][0],
        mac="c8:1b:04:e0:7e:90",
        identity="c8:1b:04:e0:7e:90",
    )
    assert await entity._post_desired_temperature(_Lk(), "c8:1b:04:e0:7e:90", 21.5) is False
