"""LK Arc Climate — writable thermostats for Arc Sense via existing LK Systems.

The stock angoyd/ha-lksystems integration often exposes only sensors for
arc-sense devices. This companion creates climate.* entities for every Arc
Sense that reports a desired temperature, and writes setpoints through the
same LK Systems coordinator (same cloud login as the LK app).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, LK_DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Auto-create the config entry once LK Systems is present.

    Copying files into custom_components is not enough — without a config
    entry no climate entities appear (dashboard shows Entity not found) and
    hemopt writes produce no LK Arc Climate log lines.
    """
    if hass.config_entries.async_entries(DOMAIN):
        return True

    if not hass.config_entries.async_entries(LK_DOMAIN):
        _LOGGER.warning(
            "LK Arc Climate: LK Systems saknas ännu — lägg till den först, "
            "sedan startas LK Arc Climate automatiskt."
        )
        return True

    _LOGGER.warning("LK Arc Climate: skapar config entry automatiskt…")
    hass.async_create_task(
        hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data={})
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up LK Arc Climate from a config entry."""
    coordinators = _lk_coordinators(hass)
    if not coordinators:
        raise ConfigEntryNotReady(
            "LK Systems-integrationen saknas eller ar inte klar. "
            "Installera/starta angoyd/ha-lksystems forst."
        )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"coordinators": coordinators}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


def _lk_coordinators(hass: HomeAssistant) -> list:
    """Return live LK Systems coordinators already configured in Home Assistant."""
    domain_data = hass.data.get(LK_DOMAIN)
    if not isinstance(domain_data, dict):
        return []

    coordinators = []
    for entry in hass.config_entries.async_entries(LK_DOMAIN):
        coordinator = domain_data.get(entry.entry_id)
        if coordinator is None:
            continue
        # Some versions nest the coordinator; accept either shape.
        if hasattr(coordinator, "data") and hasattr(coordinator, "set_thermostat_temperature"):
            coordinators.append(coordinator)
        elif isinstance(coordinator, dict):
            inner = coordinator.get("coordinator")
            if inner is not None and hasattr(inner, "set_thermostat_temperature"):
                coordinators.append(inner)
    return coordinators
