"""Config flow for WLED Segment Controller."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class WLEDSegmentControllerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WLED Segment Controller."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Select which WLED device to enhance."""
        errors: dict[str, str] = {}

        # Find all loaded WLED config entries
        wled_entries = [
            entry
            for entry in self.hass.config_entries.async_entries("wled")
            if entry.state.value == "loaded"
        ]

        if not wled_entries:
            return self.async_abort(reason="no_wled_devices")

        if user_input is not None:
            entry_id = user_input["wled_device"]
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if not entry:
                return self.async_abort(reason="device_not_found")

            host = entry.data.get("host", "")

            # Check not already configured
            await self.async_set_unique_id(f"wled_sc_{host}")
            self._abort_if_unique_id_configured()

            # Query WLED API for device info + segment names
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(
                    f"http://{host}/json",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()
            except Exception:
                errors["base"] = "cannot_connect"
                # Fall through to show form again
            else:
                device_name = data.get("info", {}).get("name", "WLED")
                segments = data.get("state", {}).get("seg", [])

                # Build segment map: {id: name}
                seg_map: dict[str, str] = {}
                for seg in segments:
                    if seg.get("stop", 0) > 0:
                        sid = str(seg.get("id", 0))
                        sname = seg.get("n", f"Segment {sid}")
                        seg_map[sid] = sname

                return self.async_create_entry(
                    title=f"{device_name}",
                    data={
                        "wled_entry_id": entry_id,
                        "host": host,
                        "device_name": device_name,
                        "segments": seg_map,
                    },
                )

        # Build device selection dropdown
        device_options = {}
        for entry in wled_entries:
            device_options[entry.entry_id] = entry.title or entry.data.get("host", "?")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("wled_device"): vol.In(device_options),
                }
            ),
            errors=errors,
        )
