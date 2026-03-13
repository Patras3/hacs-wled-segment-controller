"""WLED Segment Controller integration for Home Assistant."""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
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

ATTR_SECONDARY_COLOR = "secondary_color"
ATTR_TERTIARY_COLOR = "tertiary_color"

# Storage for saved states and pending restores
SAVED_STATES: dict[str, dict[str, Any]] = {}
PENDING_RESTORES: dict[str, dict[str, Any]] = {}

# Regex to extract segment number from entity_id
# light.wled_segment_1 → 1, light.wled_segment_2 → 2
# light.wled or light.wled_main → 0 (master = segment 0)
_SEG_RE = re.compile(r"_segment_(\d+)$")


def _entity_to_segment_id(entity_id: str) -> int:
    """Extract WLED segment ID from entity_id.
    
    light.wled_segment_1 → 1
    light.wled_segment_2 → 2
    light.wled / light.wled_main → 0
    """
    m = _SEG_RE.search(entity_id)
    if m:
        return int(m.group(1))
    # Master entity = segment 0
    return 0


def _extract_entity_ids(call: ServiceCall) -> list[str]:
    """Extract entity_ids from service call (target or data)."""
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    return entity_ids


def _build_colors(call_data: dict) -> list[list[int]] | None:
    """Build WLED color array from up to 3 color fields."""
    colors: list[list[int]] = []
    for attr in (ATTR_COLOR, ATTR_SECONDARY_COLOR, ATTR_TERTIARY_COLOR):
        raw = call_data.get(attr)
        if raw is not None:
            colors.append(parse_color(raw))
        else:
            break  # WLED expects contiguous colors
    return colors if colors else None


# Service schemas — extra=ALLOW_EXTRA for HA-injected entity_id
APPLY_EFFECT_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_COLOR): vol.Any(
            cv.string,
            vol.All(cv.ensure_list, [vol.Coerce(int)]),
        ),
        vol.Optional(ATTR_SECONDARY_COLOR): vol.Any(
            cv.string,
            vol.All(cv.ensure_list, [vol.Coerce(int)]),
        ),
        vol.Optional(ATTR_TERTIARY_COLOR): vol.Any(
            cv.string,
            vol.All(cv.ensure_list, [vol.Coerce(int)]),
        ),
        vol.Optional(ATTR_EFFECT): vol.Any(cv.string, vol.Coerce(int)),
        vol.Optional(ATTR_SPEED, default=DEFAULT_SPEED): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
        vol.Optional(ATTR_INTENSITY, default=DEFAULT_INTENSITY): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
        vol.Optional(ATTR_BRIGHTNESS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=255)
        ),
        vol.Optional(ATTR_DURATION): vol.Coerce(int),
    },
    extra=vol.ALLOW_EXTRA,
)

RESTORE_SEGMENT_SCHEMA = vol.Schema(
    {},
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

        for identifier in device.identifiers:
            if identifier[0] == "wled":
                for config_entry_id in device.config_entries:
                    ce = hass.config_entries.async_get_entry(config_entry_id)
                    if ce and ce.domain == "wled":
                        return ce.data.get("host")

        _LOGGER.error("Could not find WLED IP for entity %s", entity_id)
        return None

    async def async_get_api(entity_id: str) -> WLEDApi | None:
        """Get WLED API client for an entity."""
        host = await async_get_wled_ip(entity_id)
        if not host:
            return None
        session = async_get_clientsession(hass)
        return WLEDApi(host, session)

    async def _resolve_effect_id(api: WLEDApi, effect: Any) -> int | None:
        """Resolve effect name or ID to numeric ID."""
        if effect is None:
            return None
        if isinstance(effect, int):
            return effect
        effects_map = await api.get_effects_map()
        eid = effects_map.get(effect)
        if eid is None:
            _LOGGER.error("Effect '%s' not found on WLED", effect)
        return eid

    async def handle_apply_effect(call: ServiceCall) -> None:
        """Handle apply_effect service call.
        
        Target entity_ids determine WHICH segments to control.
        light.wled_segment_1 → segment 1
        light.wled_segment_2 → segment 2
        light.wled / light.wled_main → segment 0
        Multiple targets = multiple segments affected.
        """
        entity_ids = _extract_entity_ids(call)
        if not entity_ids:
            _LOGGER.error("No entity_id specified in target")
            return

        colors = _build_colors(call.data)
        effect = call.data.get(ATTR_EFFECT)
        speed = call.data.get(ATTR_SPEED, DEFAULT_SPEED)
        intensity = call.data.get(ATTR_INTENSITY, DEFAULT_INTENSITY)
        brightness = call.data.get(ATTR_BRIGHTNESS)
        duration = call.data.get(ATTR_DURATION)

        # Group entities by WLED host (one API instance per device)
        host_segments: dict[str, list[tuple[str, int]]] = {}
        for entity_id in entity_ids:
            host = await async_get_wled_ip(entity_id)
            if not host:
                continue
            seg_id = _entity_to_segment_id(entity_id)
            host_segments.setdefault(host, []).append((entity_id, seg_id))

        session = async_get_clientsession(hass)

        for host, segments in host_segments.items():
            api = WLEDApi(host, session)

            effect_id = await _resolve_effect_id(api, effect)
            if effect is not None and effect_id is None:
                continue

            for entity_id, segment_id in segments:
                try:
                    # Save current state if duration is set
                    if duration:
                        current_state = await api.get_segment_state(segment_id)
                        restore_key = f"{entity_id}_{segment_id}"
                        PENDING_RESTORES[restore_key] = {
                            "state": current_state,
                            "api_host": host,
                        }

                        @callback
                        def schedule_restore(
                            now: Any, key: str = restore_key
                        ) -> None:
                            hass.async_create_task(
                                async_restore_segment_state(key)
                            )

                        async_call_later(hass, duration, schedule_restore)

                    await api.apply_segment_effect(
                        segment_id,
                        colors=colors,
                        effect=effect_id,
                        speed=speed,
                        intensity=intensity,
                        brightness=brightness,
                    )

                    _LOGGER.info(
                        "Applied effect to segment %s (%s) on %s",
                        segment_id, entity_id, host,
                    )

                except WLEDApiError as err:
                    _LOGGER.error(
                        "Failed to apply effect to segment %s: %s",
                        segment_id, err,
                    )

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
            _LOGGER.info("Restored segment %s on %s", segment_id, host)
        except WLEDApiError as err:
            _LOGGER.error("Failed to restore segment: %s", err)

    async def handle_restore_segment(call: ServiceCall) -> None:
        """Handle restore_segment service call.
        
        Target entity_ids determine which segments to restore.
        """
        entity_ids = _extract_entity_ids(call)
        if not entity_ids:
            _LOGGER.error("No entity_id specified in target")
            return

        for entity_id in entity_ids:
            segment_id = _entity_to_segment_id(entity_id)
            restore_key = f"{entity_id}_{segment_id}"

            if restore_key in PENDING_RESTORES:
                await async_restore_segment_state(restore_key)
            else:
                _LOGGER.warning(
                    "No saved state for segment %s (%s)",
                    segment_id, entity_id,
                )

    async def handle_save_state(call: ServiceCall) -> None:
        """Handle save_state service call."""
        entity_ids = _extract_entity_ids(call)
        if not entity_ids:
            _LOGGER.error("No entity_id specified in target")
            return

        name = call.data[ATTR_NAME]

        # Use first entity to find the WLED host, save full state
        api = await async_get_api(entity_ids[0])
        if not api:
            return
        try:
            state = await api.get_state()
            state_key = f"{api.host}_{name}"
            SAVED_STATES[state_key] = state
            _LOGGER.info("Saved state '%s' for %s", name, api.host)
        except WLEDApiError as err:
            _LOGGER.error("Failed to save state: %s", err)

    async def handle_restore_state(call: ServiceCall) -> None:
        """Handle restore_state service call."""
        entity_ids = _extract_entity_ids(call)
        if not entity_ids:
            _LOGGER.error("No entity_id specified in target")
            return

        name = call.data[ATTR_NAME]

        api = await async_get_api(entity_ids[0])
        if not api:
            return

        state_key = f"{api.host}_{name}"
        if state_key not in SAVED_STATES:
            _LOGGER.warning("No saved state '%s' for %s", name, api.host)
            return

        try:
            saved_state = SAVED_STATES[state_key]
            await api.set_state(saved_state)
            _LOGGER.info("Restored state '%s' for %s", name, api.host)
        except WLEDApiError as err:
            _LOGGER.error("Failed to restore state: %s", err)

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_EFFECT,
        handle_apply_effect,
        schema=APPLY_EFFECT_SCHEMA,
        supports_response=SupportsResponse.NONE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_SEGMENT,
        handle_restore_segment,
        schema=RESTORE_SEGMENT_SCHEMA,
        supports_response=SupportsResponse.NONE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SAVE_STATE,
        handle_save_state,
        schema=SAVE_STATE_SCHEMA,
        supports_response=SupportsResponse.NONE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_STATE,
        handle_restore_state,
        schema=RESTORE_STATE_SCHEMA,
        supports_response=SupportsResponse.NONE,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, SERVICE_APPLY_EFFECT)
    hass.services.async_remove(DOMAIN, SERVICE_RESTORE_SEGMENT)
    hass.services.async_remove(DOMAIN, SERVICE_SAVE_STATE)
    hass.services.async_remove(DOMAIN, SERVICE_RESTORE_STATE)
    return True
