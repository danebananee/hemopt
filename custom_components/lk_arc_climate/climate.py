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
        if previous is None or (
            "measurement" in device and "measurement" not in previous
        ):
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
            # device_details often stores only measurement — merge onto known.
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
        _LOGGER.info("Skapar %d LK Arc climate-entiteter", len(entities))

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
    _attr_name = None  # primary entity → device name becomes entity_id

    def __init__(self, coordinator, device: dict, *, mac: str, identity: str) -> None:
        super().__init__(coordinator)
        self._device = device
        self._mac = mac
        self._identity = identity
        title = device.get("deviceTitle") or {}
        # zone/role kept only for logging if needed later
        self._zone = (title.get("zone") or {}).get("zoneName") or identity

        self._attr_unique_id = f"{DOMAIN}_{mac}_thermostat"
        # Same device identifiers as angoyd/ha-lksystems → thermostat
        # appears on the existing arc-sense device page (with the sensors).
        self._attr_name = "Thermostat"
        self._attr_device_info = DeviceInfo(
            identifiers={(LK_DOMAIN, identity)},
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
        # device_details may hold a newer measurement only
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
        measurement = self._live_device().get("measurement") or {}
        return _tenths_to_c(measurement.get("desiredTemperature"))

    @property
    def hvac_action(self) -> HVACAction | None:
        current = self.current_temperature
        target = self.target_temperature
        if current is None or target is None:
            return None
        return HVACAction.HEATING if current < target - 0.1 else HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        api_temp = int(round(float(temperature) * 10))
        # Azure endpoint expects the MAC; identity is accepted by some paths.
        ok = await self.coordinator.set_thermostat_temperature(self._mac, api_temp)
        if not ok and self._identity != self._mac:
            ok = await self.coordinator.set_thermostat_temperature(
                self._identity, api_temp
            )
        if not ok:
            _LOGGER.error(
                "Kunde inte satta %s till %.1f C via LK Systems", self._mac, temperature
            )
            return
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
