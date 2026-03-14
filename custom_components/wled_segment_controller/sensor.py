"""Sensor platform for WLED Segment Controller.

Creates a sensor entity per WLED segment with proper names from WLED API.
These entities serve as targets for segment controller services.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import WLEDApi, WLEDApiError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = datetime.timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WLED Segment Controller sensors from config entry."""
    host = entry.data.get("host", "")
    device_name = entry.data.get("device_name", "WLED")
    segments: dict[str, str] = entry.data.get("segments", {})

    if not host or not segments:
        _LOGGER.error("No host or segments in config entry")
        return

    entities = []
    for seg_id_str, seg_name in segments.items():
        seg_id = int(seg_id_str)
        entities.append(
            WLEDSegmentSensor(
                hass=hass,
                host=host,
                device_name=device_name,
                segment_id=seg_id,
                segment_name=seg_name,
                entry_id=entry.entry_id,
            )
        )

    async_add_entities(entities, update_before_add=True)


class WLEDSegmentSensor(SensorEntity):
    """Sensor representing a WLED segment.

    Name format: "{WLED device name} - Segment - {segment name}"
    Example: "WLED - Segment - Drzwi", "Garaż - Segment - Strip 1"
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:led-strip-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        device_name: str,
        segment_id: int,
        segment_name: str,
        entry_id: str,
    ) -> None:
        """Initialize the sensor."""
        self._hass = hass
        self._host = host
        self._device_name = device_name
        self._segment_id = segment_id
        self._segment_name = segment_name

        # Name: "WLED - Segment - Drzwi"
        self._attr_name = f"{device_name} - Segment - {segment_name}"
        self._attr_unique_id = f"wled_sc_{entry_id}_{segment_id}"

        # Initial state
        self._attr_native_value = "Unknown"

        # Attributes always available
        self._attr_extra_state_attributes: dict[str, Any] = {
            "segment_id": segment_id,
            "segment_name": segment_name,
            "wled_host": host,
            "wled_device": device_name,
        }

    @property
    def segment_id(self) -> int:
        """Return the WLED segment ID."""
        return self._segment_id

    @property
    def wled_host(self) -> str:
        """Return the WLED host."""
        return self._host

    async def async_update(self) -> None:
        """Update sensor state from WLED API."""
        try:
            session = async_get_clientsession(self._hass)
            api = WLEDApi(self._host, session)
            seg_state = await api.get_segment_state(self._segment_id)

            effect_id = seg_state.get("fx", 0)
            is_on = seg_state.get("on", False)

            # Get effect name
            effects_map = await api.get_effects_map()
            reverse_map = {v: k for k, v in effects_map.items()}
            effect_name = reverse_map.get(effect_id, f"Effect {effect_id}")

            if is_on:
                self._attr_native_value = effect_name
            else:
                self._attr_native_value = "Off"

            # Update attributes
            colors = seg_state.get("col", [])
            self._attr_extra_state_attributes.update(
                {
                    "effect": effect_name,
                    "effect_id": effect_id,
                    "brightness": seg_state.get("bri", 0),
                    "on": is_on,
                    "speed": seg_state.get("sx", 0),
                    "intensity": seg_state.get("ix", 0),
                    "colors": colors,
                }
            )
            self._attr_available = True

        except WLEDApiError as err:
            _LOGGER.debug("Failed to update segment %s: %s", self._segment_id, err)
            self._attr_available = False
        except Exception as err:
            _LOGGER.debug("Unexpected error updating segment %s: %s", self._segment_id, err)
            self._attr_available = False
