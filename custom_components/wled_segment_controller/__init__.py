"""WLED Segment Controller integration for Home Assistant."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later

from .api import WLEDApi, WLEDApiError, parse_color
from .const import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR,
    ATTR_DURATION,
    ATTR_EFFECT,
    ATTR_INTENSITY,
    ATTR_NAME,
    ATTR_SEGMENT,
    ATTR_SPEED,
    DEFAULT_INTENSITY,
    DEFAULT_SPEED,
    DOMAIN,
    SERVICE_APPLY_EFFECT,
    SERVICE_RESTORE_SEGMENT,
    SERVICE_RESTORE_STATE,
    SERVICE_SAVE_STATE,
)

_LOGGER = logging.getLogger(__name__)

# Storage for saved states and pending restores
SAVED_STATES: dict[str, dict[str, Any]] = {}
PENDING_RESTORES: dict[str, dict[str, Any]] = {}

# Service schemas
APPLY_EFFECT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SEGMENT): vol.Any(cv.string, cv.positive_int),
        vol.Optional(ATTR_COLOR): vol.Any(cv.string, vol.All(cv.ensure_list, [cv.positive_int])),
        vol.Optional(ATTR_EFFECT): vol.Any(cv.string, cv.positive_int),
        vol.Optional(ATTR_SPEED, default=DEFAULT_SPEED): vol.All(
            cv.positive_int, vol.Range(min=0, max=255)
        ),
        vol.Optional(ATTR_INTENSITY, default=DEFAULT_INTENSITY): vol.All(
            cv.positive_int, vol.Range(min=0, max=255)
        ),
        vol.Optional(ATTR_BRIGHTNESS): vol.All(
            cv.positive_int, vol.Range(min=0, max=255)
        ),
        vol.Optional(ATTR_DURATION): cv.positive_int,
    },
    extra=vol.ALLOW_EXTRA,
)

RESTORE_SEGMENT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SEGMENT): vol.Any(cv.string, cv.positive_int),
    },
    extra=vol.ALLOW_EXTRA,
)

SAVE_STATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NAME): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)

RESTORE_STATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NAME): cv.string,
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WLED Segment Controller from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    async def async_get_wled_ip(entity_id: str) -> str | None:
        """Get WLED IP address from entity's device."""
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)

        entity_entry = ent_reg.async_get(entity_id)
        if not entity_entry or not entity_entry.device_id:
            _LOGGER.error("Entity %s not found or has no device", entity_id)
            return None

        device = dev_reg.async_get(entity_entry.device_id)
        if not device:
            _LOGGER.error("Device not found for entity %s", entity_id)
            return None

        # Get IP from device identifiers or connections
        for identifier in device.identifiers:
            if identifier[0] == "wled":
                # WLED integration uses MAC as identifier, need to get config entry
                for config_entry_id in device.config_entries:
                    config_entry = hass.config_entries.async_get_entry(config_entry_id)
                    if config_entry and config_entry.domain == "wled":
                        return config_entry.data.get("host")

        _LOGGER.error("Could not find WLED IP for entity %s", entity_id)
        return None

    async def async_get_api(entity_id: str) -> WLEDApi | None:
        """Get WLED API client for an entity."""
        host = await async_get_wled_ip(entity_id)
        if not host:
            return None

        session = async_get_clientsession(hass)
        return WLEDApi(host, session)

    async def handle_apply_effect(call: ServiceCall) -> None:
        """Handle apply_effect service call."""
        entity_ids = call.data.get("entity_id", [])
        if not entity_ids:
            _LOGGER.error("No entity_id specified in target")
            return

        segment = call.data[ATTR_SEGMENT]
        color = call.data.get(ATTR_COLOR)
        effect = call.data.get(ATTR_EFFECT)
        speed = call.data.get(ATTR_SPEED, DEFAULT_SPEED)
        intensity = call.data.get(ATTR_INTENSITY, DEFAULT_INTENSITY)
        brightness = call.data.get(ATTR_BRIGHTNESS)
        duration = call.data.get(ATTR_DURATION)

        for entity_id in entity_ids:
            api = await async_get_api(entity_id)
            if not api:
                continue

            try:
                segment_id = await api.find_segment_id(segment)

                # Save current state if duration is set
                if duration:
                    current_state = await api.get_segment_state(segment_id)
                    restore_key = f"{entity_id}_{segment_id}"
                    PENDING_RESTORES[restore_key] = {
                        "state": current_state,
                        "api_host": api.host,
                    }

                    @callback
                    def schedule_restore(now: Any, key: str = restore_key) -> None:
                        """Schedule restoration of segment state."""
                        hass.async_create_task(
                            async_restore_segment_state(key)
                        )

                    async_call_later(hass, duration, schedule_restore)

                # Resolve effect name to ID if string
                effect_id = None
                if effect is not None:
                    if isinstance(effect, str):
                        effects_map = await api.get_effects_map()
                        effect_id = effects_map.get(effect)
                        if effect_id is None:
                            _LOGGER.error("Effect '%s' not found", effect)
                            continue
                    else:
                        effect_id = effect

                # Parse color
                parsed_color = None
                if color:
                    parsed_color = parse_color(color)

                await api.apply_segment_effect(
                    segment_id,
                    color=parsed_color,
                    effect=effect_id,
                    speed=speed,
                    intensity=intensity,
                    brightness=brightness,
                )

                _LOGGER.debug(
                    "Applied effect to segment %s on %s", segment, entity_id
                )

            except WLEDApiError as err:
                _LOGGER.error("Failed to apply effect: %s", err)

    async def async_restore_segment_state(restore_key: str) -> None:
        """Restore a segment to its saved state."""
        if restore_key not in PENDING_RESTORES:
            return

        restore_data = PENDING_RESTORES.pop(restore_key)
        state = restore_data["state"]
        host = restore_data["api_host"]

        session = async_get_clientsession(hass)
        api = WLEDApi(host, session)

        try:
            segment_id = state.get("id", 0)
            await api.restore_segment(segment_id, state)
            _LOGGER.debug("Restored segment %s on %s", segment_id, host)
        except WLEDApiError as err:
            _LOGGER.error("Failed to restore segment: %s", err)

    async def handle_restore_segment(call: ServiceCall) -> None:
        """Handle restore_segment service call."""
        entity_ids = call.data.get("entity_id", [])
        if not entity_ids:
            _LOGGER.error("No entity_id specified in target")
            return

        segment = call.data[ATTR_SEGMENT]

        for entity_id in entity_ids:
            api = await async_get_api(entity_id)
            if not api:
                continue

            try:
                segment_id = await api.find_segment_id(segment)
                restore_key = f"{entity_id}_{segment_id}"

                if restore_key in PENDING_RESTORES:
                    await async_restore_segment_state(restore_key)
                else:
                    _LOGGER.warning(
                        "No saved state for segment %s on %s", segment, entity_id
                    )

            except WLEDApiError as err:
                _LOGGER.error("Failed to restore segment: %s", err)

    async def handle_save_state(call: ServiceCall) -> None:
        """Handle save_state service call."""
        entity_ids = call.data.get("entity_id", [])
        if not entity_ids:
            _LOGGER.error("No entity_id specified in target")
            return

        name = call.data[ATTR_NAME]

        for entity_id in entity_ids:
            api = await async_get_api(entity_id)
            if not api:
                continue

            try:
                state = await api.get_state()
                state_key = f"{entity_id}_{name}"
                SAVED_STATES[state_key] = state
                _LOGGER.debug("Saved state '%s' for %s", name, entity_id)

            except WLEDApiError as err:
                _LOGGER.error("Failed to save state: %s", err)

    async def handle_restore_state(call: ServiceCall) -> None:
        """Handle restore_state service call."""
        entity_ids = call.data.get("entity_id", [])
        if not entity_ids:
            _LOGGER.error("No entity_id specified in target")
            return

        name = call.data[ATTR_NAME]

        for entity_id in entity_ids:
            api = await async_get_api(entity_id)
            if not api:
                continue

            state_key = f"{entity_id}_{name}"
            if state_key not in SAVED_STATES:
                _LOGGER.warning("No saved state '%s' for %s", name, entity_id)
                continue

            try:
                saved_state = SAVED_STATES[state_key]
                await api.set_state(saved_state)
                _LOGGER.debug("Restored state '%s' for %s", name, entity_id)

            except WLEDApiError as err:
                _LOGGER.error("Failed to restore state: %s", err)

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_EFFECT,
        handle_apply_effect,
        schema=APPLY_EFFECT_SCHEMA,
        supports_response=False,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_SEGMENT,
        handle_restore_segment,
        schema=RESTORE_SEGMENT_SCHEMA,
        supports_response=False,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SAVE_STATE,
        handle_save_state,
        schema=SAVE_STATE_SCHEMA,
        supports_response=False,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_STATE,
        handle_restore_state,
        schema=RESTORE_STATE_SCHEMA,
        supports_response=False,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unregister services
    hass.services.async_remove(DOMAIN, SERVICE_APPLY_EFFECT)
    hass.services.async_remove(DOMAIN, SERVICE_RESTORE_SEGMENT)
    hass.services.async_remove(DOMAIN, SERVICE_SAVE_STATE)
    hass.services.async_remove(DOMAIN, SERVICE_RESTORE_STATE)

    return True
