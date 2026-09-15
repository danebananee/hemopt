"""Climate platform — one thermostat per LK Arc Sense room unit."""

from __future__ import annotations

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
            entities.append(LKArcClimate(coordinator, device, mac=mac, identity=identity))

    if not entities:
        _LOGGER.warning(
            "Inga Arc Sense-enheter hittades. Oppna LK Systems → ladda om, "
            "eller kontrollera att rumsgivarna syns under Enheter."
        )
    else:
        for entity in entities:
            mac_slug = entity._mac.lower().replace(":", "_")  # noqa: SLF001
            _LOGGER.warning(
                "LK Arc Climate: termostat %s → climate.%s_thermostat "
                "(ANDRA DENNA — inte sensor.hemopt_setpoint_*)",
                entity._zone,  # noqa: SLF001
                mac_slug,
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
    _attr_has_entity_name = True

    def __init__(self, coordinator, device: dict, *, mac: str, identity: str) -> None:
        super().__init__(coordinator)
        self._device = device
        self._mac = mac
        self._identity = identity
        title = device.get("deviceTitle") or {}
        self._zone = (title.get("zone") or {}).get("zoneName") or identity

        self._attr_unique_id = f"{DOMAIN}_{mac}_thermostat"
        self._attr_name = "Thermostat"
        self._attr_device_info = DeviceInfo(
            identifiers={(LK_DOMAIN, identity)},
        )
        self._optimistic_target: float | None = None

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

    async def _post_desired_temperature(self, lk: Any, mac: str, celsius: float) -> bool:
        """POST link2.lk.nu …/measurement/true and verify cloud desiredTemperature.

        Uses the same endpoint as the LK app. Does not use the Azure helper —
        that can return HTTP 200 without the app ever seeing the change.
        """
        tenths = int(round(celsius * 10))
        base = self._measurement_base()

        # Prefer a fresh cloud copy when available; fall back to coordinator cache
        # so a momentary measurement GET failure does not block the write.
        if hasattr(lk, "get_device_measurement"):
            try:
                if await lk.get_device_measurement(mac, force_update=True):
                    fresh = (getattr(lk, "device_measurements", {}) or {}).get(mac) or {}
                    if fresh:
                        base = dict(fresh)
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "LK Arc Climate: kunde inte hamta measurement for %s fore skrivning",
                    mac,
                    exc_info=True,
                )

        if not base:
            _LOGGER.error(
                "LK Arc Climate: ingen measurement-data for %s — kan inte skriva",
                mac,
            )
            return False

        update_data = dict(base)
        update_data["desiredTemperature"] = tenths
        endpoint = f"service/arc/sense/{mac}/measurement/true"
        base_url = getattr(lk, "BASE_URL", "https://link2.lk.nu/")
        session = getattr(lk, "session", None)
        jwt = getattr(lk, "jwt_token", None)
        get_headers = getattr(lk, "_get_headers", None)
        if session is None or jwt is None or not callable(get_headers):
            # Older pylksystems: fall back to the helper if present.
            helper = getattr(lk, "set_device_temperature", None)
            if not callable(helper):
                _LOGGER.error("LK Arc Climate: saknar session/jwt och set_device_temperature")
                return False
            return bool(await helper(mac, celsius))

        headers = {**get_headers(), "authorization": f"Bearer {jwt}"}
        url = f"{base_url}{endpoint}"
        _LOGGER.warning(
            "LK Arc Climate: POST %s desiredTemperature=%d (%.1f C) for %s",
            endpoint,
            tenths,
            celsius,
            self._zone,
        )
        async with session.post(url, json=update_data, headers=headers) as response:
            body = await response.text()
            if response.status != 200:
                _LOGGER.error(
                    "LK Arc Climate: measurement POST HTTP %s for %s: %s",
                    response.status,
                    mac,
                    body[:300],
                )
                return False

        # Verify the cloud actually stored the new setpoint (not just HTTP 200).
        if hasattr(lk, "get_device_measurement"):
            try:
                if await lk.get_device_measurement(mac, force_update=True):
                    live = (getattr(lk, "device_measurements", {}) or {}).get(mac) or {}
                    live_tenths = live.get("desiredTemperature")
                    if live_tenths is None:
                        _LOGGER.error(
                            "LK Arc Climate: POST OK men desiredTemperature saknas i svar for %s",
                            mac,
                        )
                        return False
                    if abs(int(live_tenths) - tenths) > 0:
                        _LOGGER.error(
                            "LK Arc Climate: POST OK men molnet har %s (forvantade %d) for %s",
                            live_tenths,
                            tenths,
                            mac,
                        )
                        return False
                    apply = getattr(self.coordinator, "_apply_device_measurement", None)
                    if callable(apply):
                        apply(mac, live)
                        apply(self._identity, live)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("LK Arc Climate: kunde inte verifiera skrivning for %s", mac)
                return False

        _LOGGER.warning(
            "LK Arc Climate: OK — skrev %.1f C till %s (%s) via measurement API (verifierat)",
            celsius,
            self._zone,
            mac,
        )
        return True

    async def _write_via_measurement_api(self, device_id: str, celsius: float) -> bool:
        """POST link2.lk.nu measurement update — the path the LK app uses."""
        mac = _mac_with_colons(device_id)
        if ":" not in mac:
            _LOGGER.warning(
                "LK Arc Climate: %s ar inte en MAC med kolon — hoppar measurement API",
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

        _LOGGER.warning(
            "LK Arc Climate: forsoker satta %s (%s) till %.1f C "
            "[entity climate.%s_thermostat — inte sensor.hemopt_setpoint_*]",
            self._zone,
            self._mac,
            celsius,
            self._mac.lower().replace(":", "_"),
        )

        candidates: list[str] = []
        for value in (
            _mac_with_colons(self._mac),
            _mac_with_colons(self._identity),
            self._mac,
            self._identity,
        ):
            if value and value not in candidates:
                candidates.append(value)

        ok = False
        for device_id in candidates:
            if await self._write_via_measurement_api(device_id, celsius):
                ok = True
                break

        if not ok:
            # Do NOT fall back to the Azure helper — it can report success
            # without the LK mobile app ever seeing the change.
            _LOGGER.error(
                "LK Arc Climate: MISSLYCKADES satta %s (%s) till %.1f C. "
                "Andra climate.*_thermostat (inte sensor.hemopt_setpoint_*). "
                "Kolla att LK Systems ar inloggad och uppdaterad.",
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
                            "Se Settings → System → Logs (sok LK Arc Climate). "
                            "Obs: sensor.hemopt_setpoint_* ar bara planen — "
                            "styr via climate.*_thermostat."
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
