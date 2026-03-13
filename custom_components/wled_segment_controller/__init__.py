"""WLED Segment Controller integration for Home Assistant."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv
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
PLATFORMS = ["sensor"]

# Storage for saved states and pending restores
SAVED_STATES: dict[str, dict[str, Any]] = {}
PENDING_RESTORES: dict[str, dict[str, Any]] = {}


def _build_colors(call_data: dict) -> list[list[int]] | None:
    """Build WLED color array from up to 3 color fields."""
    colors: list[list[int]] = []
    for attr in (ATTR_COLOR, ATTR_SECONDARY_COLOR, ATTR_TERTIARY_COLOR):
        raw = call_data.get(attr)
        if raw is not None:
            colors.append(parse_color(raw))
        else:
            break
    return colors if colors else None


def _extract_entity_ids(call: ServiceCall) -> list[str]:
    """Extract entity_ids from service call."""
    entity_ids = call.data.get("entity_id", [])
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    return entity_ids


def _get_segment_info(hass: HomeAssistant, entity_id: str) -> tuple[str, int] | None:
    """Get (host, segment_id) from our sensor entity.

    Returns None if entity is not a WLED Segment Controller sensor.
    """
    state = hass.states.get(entity_id)
    if not state:
        _LOGGER.error("Entity %s not found", entity_id)
        return None

    attrs = state.attributes
    host = attrs.get("wled_host")
    seg_id = attrs.get("segment_id")

    if host is None or seg_id is None:
        _LOGGER.error(
            "Entity %s is not a WLED Segment Controller sensor "
            "(missing wled_host/segment_id attributes)",
            entity_id,
        )
        return None

    return (host, int(seg_id))


# Service schemas
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

RESTORE_SEGMENT_SCHEMA = vol.Schema({}, extra=vol.ALLOW_EXTRA)

SAVE_STATE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_NAME): cv.string},
    extra=vol.ALLOW_EXTRA,
)

RESTORE_STATE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_NAME): cv.string},
    extra=vol.ALLOW_EXTRA,
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WLED Segment Controller from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Set up sensor platform (creates segment entities)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    session = async_get_clientsession(hass)

    async def _resolve_effect_id(api: WLEDApi, effect: Any) -> int | None:
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

        Target entity_ids are our sensor entities (e.g. sensor.wled_dom_drzwi).
        Each entity has wled_host and segment_id attributes.
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

        # Group by host for efficiency
        host_segments: dict[str, list[tuple[str, int]]] = {}
        for eid in entity_ids:
            info = _get_segment_info(hass, eid)
            if info:
                host, seg_id = info
                host_segments.setdefault(host, []).append((eid, seg_id))

        for host, segments in host_segments.items():
            api = WLEDApi(host, session)

            effect_id = await _resolve_effect_id(api, effect)
            if effect is not None and effect_id is None:
                continue

            for entity_id, segment_id in segments:
                try:
                    if duration:
                        current_state = await api.get_segment_state(segment_id)
                        restore_key = f"{host}_{segment_id}"
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
                        "Applied effect to segment %s on %s", segment_id, host
                    )

                except WLEDApiError as err:
                    _LOGGER.error("Failed: segment %s: %s", segment_id, err)

    async def async_restore_segment_state(restore_key: str) -> None:
        """Restore a segment to its saved state."""
        if restore_key not in PENDING_RESTORES:
            return

        restore_data = PENDING_RESTORES.pop(restore_key)
        state = restore_data["state"]
        host = restore_data["api_host"]
        api = WLEDApi(host, session)

        try:
            segment_id = state.get("id", 0)
            await api.restore_segment(segment_id, state)
            _LOGGER.info("Restored segment %s on %s", segment_id, host)
        except WLEDApiError as err:
            _LOGGER.error("Failed to restore segment: %s", err)

    async def handle_restore_segment(call: ServiceCall) -> None:
        """Handle restore_segment service call."""
        entity_ids = _extract_entity_ids(call)
        if not entity_ids:
            return

        for eid in entity_ids:
            info = _get_segment_info(hass, eid)
            if not info:
                continue
            host, seg_id = info
            restore_key = f"{host}_{seg_id}"
            if restore_key in PENDING_RESTORES:
                await async_restore_segment_state(restore_key)
            else:
                _LOGGER.warning("No saved state for segment %s on %s", seg_id, host)

    async def handle_save_state(call: ServiceCall) -> None:
        """Handle save_state service call."""
        entity_ids = _extract_entity_ids(call)
        if not entity_ids:
            return

        name = call.data[ATTR_NAME]
        info = _get_segment_info(hass, entity_ids[0])
        if not info:
            return

        host = info[0]
        api = WLEDApi(host, session)
        try:
            state = await api.get_state()
            SAVED_STATES[f"{host}_{name}"] = state
            _LOGGER.info("Saved state '%s' for %s", name, host)
        except WLEDApiError as err:
            _LOGGER.error("Failed to save state: %s", err)

    async def handle_restore_state(call: ServiceCall) -> None:
        """Handle restore_state service call."""
        entity_ids = _extract_entity_ids(call)
        if not entity_ids:
            return

        name = call.data[ATTR_NAME]
        info = _get_segment_info(hass, entity_ids[0])
        if not info:
            return

        host = info[0]
        key = f"{host}_{name}"
        if key not in SAVED_STATES:
            _LOGGER.warning("No saved state '%s' for %s", name, host)
            return

        api = WLEDApi(host, session)
        try:
            await api.set_state(SAVED_STATES[key])
            _LOGGER.info("Restored state '%s' for %s", name, host)
        except WLEDApiError as err:
            _LOGGER.error("Failed to restore state: %s", err)

    # Register services
    hass.services.async_register(
        DOMAIN, SERVICE_APPLY_EFFECT, handle_apply_effect,
        schema=APPLY_EFFECT_SCHEMA,
        supports_response=SupportsResponse.NONE,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESTORE_SEGMENT, handle_restore_segment,
        schema=RESTORE_SEGMENT_SCHEMA,
        supports_response=SupportsResponse.NONE,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SAVE_STATE, handle_save_state,
        schema=SAVE_STATE_SCHEMA,
        supports_response=SupportsResponse.NONE,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESTORE_STATE, handle_restore_state,
        schema=RESTORE_STATE_SCHEMA,
        supports_response=SupportsResponse.NONE,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.services.async_remove(DOMAIN, SERVICE_APPLY_EFFECT)
    hass.services.async_remove(DOMAIN, SERVICE_RESTORE_SEGMENT)
    hass.services.async_remove(DOMAIN, SERVICE_SAVE_STATE)
    hass.services.async_remove(DOMAIN, SERVICE_RESTORE_STATE)
    return True
