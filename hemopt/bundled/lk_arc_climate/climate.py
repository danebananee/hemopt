"""Climate platform — one thermostat per LK Arc Sense room unit."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LK_DOMAIN, MAX_TEMP, MIN_TEMP, TEMP_STEP

_LOGGER = logging.getLogger(__name__)


def _iter_arc_sense(coordinator) -> list[dict]:
    """Collect Arc Sense devices from every list the coordinator keeps."""
    data = coordinator.data or {}
    found: dict[str, dict] = {}

    def consider(device: dict) -> None:
        title = device.get("deviceTitle") or {}
        if title.get("deviceGroup") != "arc":
            return
        if title.get("deviceType") != "arc-sense":
            return
        mac = device.get("mac") or title.get("identity")
        if not mac:
            return
        # Prefer the copy that already has measurement data.
        previous = found.get(mac)
        if previous is None or ("measurement" in device and "measurement" not in previous):
            found[mac] = device

    for device in data.get("devices") or []:
        if isinstance(device, dict):
            consider(device)

    hub_data = data.get("hub_data") or {}
    if isinstance(hub_data, dict):
        for hub in hub_data.values():
            if not isinstance(hub, dict):
                continue
            for device in hub.get("devices") or []:
                if isinstance(device, dict):
                    consider(device)

    details = data.get("device_details") or {}
    if isinstance(details, dict):
        for identity, detail in details.items():
            if not isinstance(detail, dict):
                continue
            mac = identity
            if mac in found and "measurement" in detail:
                found[mac] = {**found[mac], "measurement": detail["measurement"]}

    return list(found.values())


def _tenths_to_c(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 10.0
    except (TypeError, ValueError):
        return None


def _mac_with_colons(value: str) -> str:
    """LK measurement API requires a colon-separated MAC (aa:bb:…)."""
    if not value:
        return value
    if ":" in value:
        return value.replace("-", ":").lower()
    hex_only = "".join(ch for ch in value.lower().replace("-", "").replace("_", "") if ch.isalnum())
    if len(hex_only) == 12:
        return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
    return value


def _mac_slug(value: str) -> str:
    """HA entity id fragment: e0_ec_2c_c8_5e_2c (same as ha-lksystems sensors)."""
    return _mac_with_colons(value).replace(":", "_")


def _desired_entity_id(mac: str) -> str:
    return f"climate.{_mac_slug(mac)}_thermostat"


def _migrate_registry_ids(hass: HomeAssistant, mac: str, identity: str) -> None:
    """Force climate.<mac_slug>_thermostat even if an older unique_id created another id."""
    registry = er.async_get(hass)
    mac_slug = _mac_slug(mac)
    desired = _desired_entity_id(mac)
    desired_uid = f"{DOMAIN}_{mac_slug}_thermostat"
    candidates = {
        desired_uid,
        f"{DOMAIN}_{mac_slug}",
        f"{DOMAIN}_{mac}_thermostat",
        f"{DOMAIN}_{mac}",
        f"{DOMAIN}_{identity}_thermostat",
        f"{DOMAIN}_{identity}",
        f"{DOMAIN}_{_mac_with_colons(mac)}_thermostat",
        f"{DOMAIN}_{_mac_with_colons(mac)}",
    }
    for uid in candidates:
        entry = registry.async_get_entity_id("climate", DOMAIN, uid)
        if not entry:
            continue
        updates: dict[str, Any] = {}
        if entry != desired:
            updates["new_entity_id"] = desired
        reg_entry = registry.async_get(entry)
        if reg_entry and reg_entry.unique_id != desired_uid:
            updates["new_unique_id"] = desired_uid
        if updates:
            try:
                registry.async_update_entity(entry, **updates)
                _LOGGER.warning(
                    "LK Arc Climate: registry %s → %s (%s)",
                    entry,
                    desired,
                    updates,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception("LK Arc Climate: kunde inte byta %s till %s", entry, desired)

    # Also catch a bare climate.<mac_slug> left behind by an older pin.
    legacy = f"climate.{mac_slug}"
    if legacy != desired:
        reg_entry = registry.async_get(legacy)
        if reg_entry and reg_entry.platform == DOMAIN:
            try:
                registry.async_update_entity(
                    legacy, new_entity_id=desired, new_unique_id=desired_uid
                )
                _LOGGER.warning("LK Arc Climate: registry %s → %s (legacy id)", legacy, desired)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("LK Arc Climate: kunde inte byta %s till %s", legacy, desired)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Discover Arc Sense units and expose them as climate entities."""
    store = hass.data[DOMAIN][entry.entry_id]
    entities: list[LKArcClimate] = []
    seen: set[str] = set()

    for coordinator in store["coordinators"]:
        for device in _iter_arc_sense(coordinator):
            title = device.get("deviceTitle") or {}
            identity = title.get("identity") or device.get("mac")
            mac = device.get("mac") or identity
            if not mac or mac in seen:
                continue
            seen.add(mac)
            _migrate_registry_ids(hass, mac, identity or mac)
            entities.append(LKArcClimate(coordinator, device, mac=mac, identity=identity))

    if not entities:
        _LOGGER.warning(
            "Inga Arc Sense-enheter hittades. Oppna LK Systems → ladda om, "
            "eller kontrollera att rumsgivarna syns under Enheter."
        )
    else:
        _LOGGER.info(
            "LK Arc Climate: %d termostater skapade (styr climate.*_thermostat, "
            "inte sensor.hemopt_setpoint_*)",
            len(entities),
        )
        for entity in entities:
            _LOGGER.info(
                "LK Arc Climate: termostat %s → %s",
                entity._zone,  # noqa: SLF001
                entity.entity_id,
            )

    async_add_entities(entities)


class LKArcClimate(CoordinatorEntity, ClimateEntity):
    """Writable room thermostat backed by the LK Systems cloud API."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_target_temperature_step = TEMP_STEP
    # Keep names simple so HA does not rewrite entity_id from the device name.
    _attr_has_entity_name = False

    def __init__(self, coordinator, device: dict, *, mac: str, identity: str) -> None:
        super().__init__(coordinator)
        self._device = device
        self._mac = mac
        self._identity = identity
        title = device.get("deviceTitle") or {}
        self._zone = (title.get("zone") or {}).get("zoneName") or identity

        mac_slug = _mac_slug(mac)
        self._desired_entity_id = _desired_entity_id(mac)
        # Pin the entity_id so dashboards / hemopt.yaml stay stable
        # (climate.e0_ec_2c_c8_5e_2c_thermostat — same slug as the sensors).
        self.entity_id = self._desired_entity_id
        self._attr_unique_id = f"{DOMAIN}_{mac_slug}_thermostat"
        self._attr_name = self._zone
        self._attr_device_info = DeviceInfo(
            identifiers={(LK_DOMAIN, identity)},
        )
        self._optimistic_target: float | None = None
        self._write_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.entity_id == self._desired_entity_id:
            return
        registry = er.async_get(self.hass)
        try:
            registry.async_update_entity(self.entity_id, new_entity_id=self._desired_entity_id)
            _LOGGER.warning(
                "LK Arc Climate: entity_id %s → %s (dashboard/hemopt)",
                self.entity_id,
                self._desired_entity_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "LK Arc Climate: kunde inte låsa entity_id till %s (nu: %s)",
                self._desired_entity_id,
                self.entity_id,
            )

    def _live_device(self) -> dict:
        """Fresh device dict from the coordinator, falling back to setup copy."""
        data = self.coordinator.data or {}
        for device in _iter_arc_sense(self.coordinator):
            title = device.get("deviceTitle") or {}
            mac = device.get("mac") or title.get("identity")
            identity = title.get("identity") or mac
            if mac == self._mac or identity == self._identity:
                return device
        details = (data.get("device_details") or {}).get(self._identity) or {}
        if details.get("measurement"):
            merged = dict(self._device)
            merged["measurement"] = details["measurement"]
            return merged
        return self._device

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success)

    @property
    def current_temperature(self) -> float | None:
        measurement = self._live_device().get("measurement") or {}
        return _tenths_to_c(measurement.get("currentTemperature"))

    @property
    def target_temperature(self) -> float | None:
        if self._optimistic_target is not None:
            return self._optimistic_target
        measurement = self._live_device().get("measurement") or {}
        return _tenths_to_c(measurement.get("desiredTemperature"))

    @property
    def hvac_action(self) -> HVACAction | None:
        current = self.current_temperature
        target = self.target_temperature
        if current is None or target is None:
            return None
        return HVACAction.HEATING if current < target - 0.1 else HVACAction.IDLE

    def _measurement_base(self) -> dict[str, Any]:
        """Best available measurement dict to POST back with a new setpoint."""
        return dict(self._live_device().get("measurement") or {})

    def _lk_auth(self, lk: Any) -> tuple[Any, dict[str, str]] | None:
        session = getattr(lk, "session", None)
        jwt = getattr(lk, "jwt_token", None)
        get_headers = getattr(lk, "_get_headers", None)
        if session is None or not jwt or not callable(get_headers):
            return None
        headers = {**get_headers(), "authorization": f"Bearer {jwt}"}
        return session, headers

    @staticmethod
    def _request_timeout():
        """Per-request timeout (pylksystems session defaults to 20s — too short)."""
        try:
            from aiohttp import ClientTimeout

            return ClientTimeout(total=60, sock_connect=15, sock_read=45)
        except ImportError:  # pragma: no cover - unit tests without aiohttp
            return 60

    async def _verify_desired(self, lk: Any, mac: str, tenths: int) -> bool:
        """Re-read measurement; tolerate a short cloud lag."""
        if not hasattr(lk, "get_device_measurement"):
            return True
        for attempt in range(3):
            try:
                if await lk.get_device_measurement(mac, force_update=True):
                    live = (getattr(lk, "device_measurements", {}) or {}).get(mac) or {}
                    live_tenths = live.get("desiredTemperature")
                    if live_tenths is not None and abs(int(live_tenths) - tenths) == 0:
                        apply = getattr(self.coordinator, "_apply_device_measurement", None)
                        if callable(apply):
                            apply(mac, live)
                            apply(self._identity, live)
                        return True
                    _LOGGER.debug(
                        "LK Arc Climate: verify attempt %d — molnet har %s (forvantade %d)",
                        attempt + 1,
                        live_tenths,
                        tenths,
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "LK Arc Climate: verify attempt %d misslyckades",
                    attempt + 1,
                    exc_info=True,
                )
            if attempt < 2:
                await asyncio.sleep(0.7)
        return False

    async def _post_via_control_api(self, lk: Any, mac: str, tenths: int) -> bool:
        """POST link2.lk.nu/control/arc/sense/{mac}/temperature (official write path)."""
        auth = self._lk_auth(lk)
        if auth is None:
            return False
        session, headers = auth
        url = f"https://link2.lk.nu/control/arc/sense/{mac}/temperature"
        payload = {"temperature": tenths}
        _LOGGER.info(
            "LK Arc Climate: POST control …/temperature %s → %d (%.1f C) for %s",
            mac,
            tenths,
            tenths / 10.0,
            self._zone,
        )
        try:
            async with session.post(
                url, json=payload, headers=headers, timeout=self._request_timeout()
            ) as response:
                body = await response.text()
                if response.status not in (200, 204):
                    _LOGGER.error(
                        "LK Arc Climate: control POST HTTP %s for %s: %s",
                        response.status,
                        mac,
                        body[:300],
                    )
                    return False
                if response.status == 200 and body:
                    try:
                        data = json.loads(body)
                        returned = data.get("temperature")
                        if returned is not None and abs(int(returned) - tenths) > 0:
                            _LOGGER.error(
                                "LK Arc Climate: control svarade temperature=%s (forvantade %d)",
                                returned,
                                tenths,
                            )
                            return False
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
        except TimeoutError:
            # Server may still have applied the write; confirm via measurement.
            _LOGGER.warning(
                "LK Arc Climate: control POST timeout for %s — verifierar via measurement",
                mac,
            )
            if await self._verify_desired(lk, mac, tenths):
                _LOGGER.info(
                    "LK Arc Climate: OK — %.1f C till %s efter timeout (verifierat)",
                    tenths / 10.0,
                    self._zone,
                )
                return True
            return False

        if await self._verify_desired(lk, mac, tenths):
            _LOGGER.info(
                "LK Arc Climate: OK — skrev %.1f C till %s (%s) via control API",
                tenths / 10.0,
                self._zone,
                mac,
            )
            return True
        # Control accepted the write (200/204); measurement poll can lag — still OK.
        _LOGGER.info(
            "LK Arc Climate: OK — control API accepterade %.1f C for %s "
            "(measurement-verify laggar; antar OK)",
            tenths / 10.0,
            self._zone,
        )
        return True

    async def _post_via_measurement_api(self, lk: Any, mac: str, tenths: int) -> bool:
        """Legacy pylksystems path: POST …/service/…/measurement/true."""
        auth = self._lk_auth(lk)
        if auth is None:
            helper = getattr(lk, "set_device_temperature", None)
            if callable(helper):
                return bool(await helper(mac, tenths / 10.0))
            return False
        session, headers = auth

        base = self._measurement_base()
        if hasattr(lk, "get_device_measurement"):
            try:
                if await lk.get_device_measurement(mac, force_update=True):
                    fresh = (getattr(lk, "device_measurements", {}) or {}).get(mac) or {}
                    if fresh:
                        base = dict(fresh)
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "LK Arc Climate: kunde inte hamta measurement for %s",
                    mac,
                    exc_info=True,
                )
        if not base:
            _LOGGER.error(
                "LK Arc Climate: ingen measurement-data for %s — hoppar legacy POST",
                mac,
            )
            return False

        update_data = dict(base)
        update_data["desiredTemperature"] = tenths
        base_url = getattr(lk, "BASE_URL", "https://link2.lk.nu/")
        url = f"{base_url}service/arc/sense/{mac}/measurement/true"
        _LOGGER.info(
            "LK Arc Climate: POST measurement/true desiredTemperature=%d for %s",
            tenths,
            self._zone,
        )
        try:
            async with session.post(
                url, json=update_data, headers=headers, timeout=self._request_timeout()
            ) as response:
                body = await response.text()
                if response.status != 200:
                    _LOGGER.error(
                        "LK Arc Climate: measurement POST HTTP %s for %s: %s",
                        response.status,
                        mac,
                        body[:300],
                    )
                    return False
        except TimeoutError:
            _LOGGER.warning(
                "LK Arc Climate: measurement POST timeout for %s — verifierar",
                mac,
            )
            if await self._verify_desired(lk, mac, tenths):
                _LOGGER.info(
                    "LK Arc Climate: OK — %.1f C till %s efter measurement-timeout",
                    tenths / 10.0,
                    self._zone,
                )
                return True
            return False
        if not await self._verify_desired(lk, mac, tenths):
            _LOGGER.error(
                "LK Arc Climate: measurement POST OK men molnet verifierades inte for %s",
                mac,
            )
            return False
        _LOGGER.info(
            "LK Arc Climate: OK — skrev %.1f C till %s (%s) via measurement API",
            tenths / 10.0,
            self._zone,
            mac,
        )
        return True

    async def _post_desired_temperature(self, lk: Any, mac: str, celsius: float) -> bool:
        """Write setpoint: official control API first, then legacy measurement POST."""
        tenths = int(round(celsius * 10))
        if await self._post_via_control_api(lk, mac, tenths):
            return True
        _LOGGER.warning(
            "LK Arc Climate: control API misslyckades for %s — provar measurement/true",
            mac,
        )
        return await self._post_via_measurement_api(lk, mac, tenths)

    async def _write_via_measurement_api(self, device_id: str, celsius: float) -> bool:
        """Authenticate via LK Systems coordinator and write the setpoint."""
        mac = _mac_with_colons(device_id)
        if ":" not in mac:
            _LOGGER.warning(
                "LK Arc Climate: %s ar inte en MAC med kolon — hoppar skrivning",
                device_id,
            )
            return False

        client_cm = getattr(self.coordinator, "_authenticated_client", None)
        if client_cm is None:
            _LOGGER.error(
                "LK Arc Climate: LK Systems-coordinator saknar _authenticated_client "
                "(for gammal ha-lksystems?)"
            )
            return False

        try:
            async with client_cm() as lk:
                return await self._post_desired_temperature(lk, mac, celsius)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "LK Arc Climate: exception vid skrivning till %s (%s) → %.1f C",
                self._zone,
                mac,
                celsius,
            )
            return False

    def _apply_local_desired(self, celsius: float) -> None:
        """Patch coordinator cache so the UI shows the new setpoint immediately."""
        tenths = int(round(celsius * 10))
        apply = getattr(self.coordinator, "_apply_device_measurement", None)
        measurement = dict(self._live_device().get("measurement") or {})
        measurement["desiredTemperature"] = tenths
        if callable(apply):
            try:
                apply(self._identity, measurement)
                apply(self._mac, measurement)
                return
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Kunde inte patcha coordinator-cache", exc_info=True)
        device = self._live_device()
        device.setdefault("measurement", {})["desiredTemperature"] = tenths

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        celsius = float(temperature)

        async with self._write_lock:
            await self._async_set_temperature_locked(celsius)

    async def _async_set_temperature_locked(self, celsius: float) -> None:
        _LOGGER.info(
            "LK Arc Climate: forsoker satta %s (%s) till %.1f C [%s]",
            self._zone,
            self._mac,
            celsius,
            self.entity_id,
        )

        # Prefer colon-MAC once — duplicates only wasted time against the 20–60s timeout.
        candidates: list[str] = []
        for value in (_mac_with_colons(self._mac), _mac_with_colons(self._identity)):
            if value and ":" in value and value not in candidates:
                candidates.append(value)

        ok = False
        for device_id in candidates:
            if await self._write_via_measurement_api(device_id, celsius):
                ok = True
                break

        if not ok:
            _LOGGER.error(
                "LK Arc Climate: MISSLYCKADES satta %s (%s) till %.1f C. "
                "Kolla Settings → System → Logs (control timeout / HTTP-fel).",
                self._zone,
                self._mac,
                celsius,
            )
            self._optimistic_target = None
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "LK Arc Climate",
                        "message": (
                            f"Kunde inte skriva {celsius:.1f} °C till {self._zone}. "
                            "Se Settings → System → Logs (sok LK Arc Climate)."
                        ),
                        "notification_id": f"lk_arc_climate_{self._mac}",
                    },
                )
            )
            return

        self._optimistic_target = celsius
        self._apply_local_desired(celsius)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._optimistic_target is not None:
            measurement = self._live_device().get("measurement") or {}
            live = _tenths_to_c(measurement.get("desiredTemperature"))
            if live is not None and abs(live - self._optimistic_target) < 0.05:
                self._optimistic_target = None
        self.async_write_ha_state()
