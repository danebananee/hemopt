"""Config flow for LK Arc Climate."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, LK_DOMAIN


async def _lk_ready(hass: HomeAssistant) -> bool:
    return bool(hass.config_entries.async_entries(LK_DOMAIN)) and LK_DOMAIN in hass.data


class LKArcClimateConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """One-shot setup that reuses the existing LK Systems login.

    No form — hemopt and HA startup can create the entry automatically.
    """

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        return await self._create()

    async def async_step_import(self, user_input=None) -> FlowResult:
        return await self._create()

    async def async_step_discovery(self, discovery_info=None) -> FlowResult:
        return await self._create()

    async def _create(self) -> FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        if not await _lk_ready(self.hass):
            return self.async_abort(reason="missing_lksystems")

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="LK Arc Climate", data={})
