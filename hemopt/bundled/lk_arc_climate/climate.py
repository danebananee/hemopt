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


def _mac_with_colons(value: str) -> str:
    """LK measurement API requires a colon-separated MAC (aa:bb:…)."""
    raw = (value or "").strip().replace("-", ":").replace("_", "").lower()
    if ":" in (value or ""):
        return value.replace("-", ":").lower()
    hex_only = "".join(ch for ch in raw if ch.isalnum())
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

    async def _write_setpoint(self, device_id: str, celsius: float) -> bool:
        """Write desired temperature the same way the LK app does.

        Prefer ``set_device_temperature`` (POST link2.lk.nu
        ``service/arc/sense/<mac>/measurement/true``). The Azure helper that
        ``coordinator.set_thermostat_temperature`` uses can return OK without
        the mobile app ever seeing the change.
        """
        mac = _mac_with_colons(device_id)
        client_cm = getattr(self.coordinator, "_authenticated_client", None)
        if client_cm is not None:
            try:
                async with client_cm() as lk:
                    if hasattr(lk, "set_device_temperature") and ":" in mac:
                        if await lk.set_device_temperature(mac, celsius):
                            _LOGGER.info(
                                "LK Arc Climate: skrev %.1f C till %s via measurement API",
                                celsius,
                                mac,
                            )
                            return True
                        _LOGGER.warning(
                            "LK Arc Climate: measurement API nekade %.1f C for %s",
                            celsius,
                            mac,
                        )
                    # Fall back to Azure helper on the same authenticated client.
                    if hasattr(lk, "set_thermostat_temperature"):
                        result = await lk.set_thermostat_temperature(mac, int(round(celsius * 10)))
                        ok = (
                            bool(result.get("success"))
                            if isinstance(result, dict)
                            else bool(result)
                        )
                        if ok:
                            _LOGGER.info(
                                "LK Arc Climate: skrev %.1f C till %s via Azure API",
                                celsius,
                                mac,
                            )
                            return True
            except Exception:  # noqa: BLE001 - surface in log, try next path
                _LOGGER.exception(
                    "LK Arc Climate: fel vid skrivning till %s (%.1f C)", mac, celsius
                )

        # Last resort: public coordinator wrapper (Azure, tenths).
        try:
            return bool(
                await self.coordinator.set_thermostat_temperature(mac, int(round(celsius * 10)))
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("LK Arc Climate: coordinator-skrivning misslyckades for %s", mac)
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

        candidates = []
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
            if await self._write_setpoint(device_id, celsius):
                ok = True
                break

        if not ok:
            _LOGGER.error(
                "LK Arc Climate: kunde INTE satta %s (%s) till %.1f C — "
                "kolla Settings → System → Logs for lk_arc_climate / lksystems",
                self._zone,
                self._mac,
                celsius,
            )
            self._optimistic_target = None
            return

        self._optimistic_target = celsius
        self._apply_local_desired(celsius)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        # Drop optimistic value once the cloud measurement catches up.
        if self._optimistic_target is not None:
            measurement = self._live_device().get("measurement") or {}
            live = _tenths_to_c(measurement.get("desiredTemperature"))
            if live is not None and abs(live - self._optimistic_target) < 0.05:
                self._optimistic_target = None
        self.async_write_ha_state()
