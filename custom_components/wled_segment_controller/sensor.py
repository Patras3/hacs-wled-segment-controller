"""Sensor platform for WLED Segment Controller.

Creates a sensor entity per WLED segment with proper names.
Uses a shared coordinator to poll WLED once for all segments.
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
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import WLEDApi, WLEDApiError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = datetime.timedelta(seconds=30)


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

    session = async_get_clientsession(hass)
    api = WLEDApi(host, session)

    async def _async_update_data() -> dict[str, Any]:
        """Fetch full WLED state once for all segments."""
        try:
            state = await api.get_state()
            effects_map = await api.get_effects_map()
            reverse_effects = {v: k for k, v in effects_map.items()}
            return {
                "segments": {s.get("id", i): s for i, s in enumerate(state.get("seg", []))},
                "effects": reverse_effects,
                "on": state.get("on", False),
                "bri": state.get("bri", 0),
            }
        except WLEDApiError as err:
            raise UpdateFailed(f"Error communicating with WLED: {err}") from err

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"WLED Segment Controller ({device_name})",
        update_method=_async_update_data,
        update_interval=UPDATE_INTERVAL,
    )

    # Initial fetch
    await coordinator.async_config_entry_first_refresh()

    entities = []
    for seg_id_str, seg_name in segments.items():
        seg_id = int(seg_id_str)
        entities.append(
            WLEDSegmentSensor(
                coordinator=coordinator,
                host=host,
                device_name=device_name,
                segment_id=seg_id,
                segment_name=seg_name,
                entry_id=entry.entry_id,
            )
        )

    async_add_entities(entities)


class WLEDSegmentSensor(CoordinatorEntity, SensorEntity):
    """Sensor representing a WLED segment.

    Name format: "{HA device title} - Segment - {segment name}"
    Example: "Dom - Segment - Drzwi", "Garaż - Segment - Strip 1"
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:led-strip-variant"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        host: str,
        device_name: str,
        segment_id: int,
        segment_name: str,
        entry_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._host = host
        self._device_name = device_name
        self._segment_id = segment_id
        self._segment_name = segment_name

        # Name: "Dom - Segment - Drzwi"
        self._attr_name = f"{device_name} - Segment - {segment_name}"
        self._attr_unique_id = f"wled_sc_{entry_id}_{segment_id}"

    @property
    def segment_id(self) -> int:
        """Return the WLED segment ID."""
        return self._segment_id

    @property
    def wled_host(self) -> str:
        """Return the WLED host."""
        return self._host

    @property
    def native_value(self) -> str | None:
        """Return the current effect name."""
        if not self.coordinator.data:
            return None

        seg = self.coordinator.data.get("segments", {}).get(self._segment_id)
        if not seg:
            return None

        is_on = seg.get("on", False)
        if not is_on:
            return "Off"

        effect_id = seg.get("fx", 0)
        effects = self.coordinator.data.get("effects", {})
        return effects.get(effect_id, f"Effect {effect_id}")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity attributes."""
        attrs: dict[str, Any] = {
            "segment_id": self._segment_id,
            "segment_name": self._segment_name,
            "wled_host": self._host,
            "wled_device": self._device_name,
        }

        if self.coordinator.data:
            seg = self.coordinator.data.get("segments", {}).get(self._segment_id)
            if seg:
                effect_id = seg.get("fx", 0)
                effects = self.coordinator.data.get("effects", {})
                attrs.update(
                    {
                        "effect": effects.get(effect_id, f"Effect {effect_id}"),
                        "effect_id": effect_id,
                        "brightness": seg.get("bri", 0),
                        "on": seg.get("on", False),
                        "speed": seg.get("sx", 0),
                        "intensity": seg.get("ix", 0),
                        "colors": seg.get("col", []),
                    }
                )

        return attrs
