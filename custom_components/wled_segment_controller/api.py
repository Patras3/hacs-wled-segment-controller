"""Async WLED JSON API client."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
from aiohttp import ClientTimeout

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = ClientTimeout(total=10)


class WLEDApiError(Exception):
    """WLED API error."""


class WLEDApi:
    """Async client for WLED JSON API."""

    def __init__(self, host: str, session: aiohttp.ClientSession) -> None:
        """Initialize the API client."""
        self._host = host
        self._session = session
        self._base_url = f"http://{host}"
        self._effects_cache: dict[str, int] | None = None

    @property
    def host(self) -> str:
        """Return the WLED host."""
        return self._host

    async def get_full_state(self) -> dict[str, Any]:
        """Get full WLED state including info, state, effects, palettes."""
        return await self._get("/json")

    async def get_state(self) -> dict[str, Any]:
        """Get current WLED state with segments."""
        return await self._get("/json/state")

    async def set_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update WLED state (partial updates supported)."""
        return await self._post("/json/state", payload)

    async def get_effects_map(self, force_refresh: bool = False) -> dict[str, int]:
        """Get effect name to ID mapping. Cached per instance."""
        if self._effects_cache is None or force_refresh:
            data = await self.get_full_state()
            effects = data.get("effects", [])
            self._effects_cache = {name: idx for idx, name in enumerate(effects)}
        return self._effects_cache

    async def find_segment_id(self, segment: int | str) -> int:
        """Find segment ID by name or return directly if int."""
        if isinstance(segment, int):
            return segment

        state = await self.get_state()
        segments = state.get("seg", [])

        for seg in segments:
            if seg.get("n") == segment:
                return seg.get("id", segments.index(seg))

        raise WLEDApiError(f"Segment '{segment}' not found")

    async def get_segment_state(self, segment_id: int) -> dict[str, Any]:
        """Get current state of a specific segment."""
        state = await self.get_state()
        segments = state.get("seg", [])

        for seg in segments:
            if seg.get("id") == segment_id:
                return seg

        raise WLEDApiError(f"Segment ID {segment_id} not found")

    async def is_on(self) -> bool:
        """Check if WLED master is on."""
        state = await self.get_state()
        return state.get("on", False)

    async def get_segments_on_state(self) -> dict[int, bool]:
        """Get on/off state of all segments."""
        state = await self.get_state()
        return {
            seg.get("id", i): seg.get("on", False)
            for i, seg in enumerate(state.get("seg", []))
            if seg.get("stop", 1) > 0  # skip empty segments
        }

    async def set_master_on(self, on: bool) -> dict[str, Any]:
        """Turn WLED master on or off."""
        return await self.set_state({"on": on})

    async def set_segments_on(
        self, segments: dict[int, bool]
    ) -> dict[str, Any]:
        """Set on/off state for multiple segments at once."""
        seg_data = [{"id": sid, "on": on} for sid, on in segments.items()]
        return await self.set_state({"seg": seg_data})

    async def apply_segment_effect(
        self,
        segment_id: int,
        *,
        color: list[int] | None = None,
        colors: list[list[int]] | None = None,
        effect: int | None = None,
        speed: int | None = None,
        intensity: int | None = None,
        brightness: int | None = None,
    ) -> dict[str, Any]:
        """Apply effect settings to a specific segment.

        Sends a single atomic request that:
        1. Turns on the WLED master (if needed)
        2. Turns on the target segment with the effect
        3. Preserves other segments' current on/off state

        This prevents the race condition where WLED briefly enables all
        segments between master power-on and individual segment control.

        Args:
            colors: List of up to 3 RGB colors [[R,G,B], [R,G,B], [R,G,B]]
            color: Single RGB color (legacy, use colors for multi)
        """
        # Read current state to preserve other segments
        current = await self.get_state()
        current_segments = current.get("seg", [])

        seg_data: dict[str, Any] = {"id": segment_id, "on": True}

        if colors is not None:
            seg_data["col"] = colors
        elif color is not None:
            seg_data["col"] = [color]
        if effect is not None:
            seg_data["fx"] = effect
        if speed is not None:
            seg_data["sx"] = speed
        if intensity is not None:
            seg_data["ix"] = intensity
        if brightness is not None:
            seg_data["bri"] = brightness

        # Build atomic payload: master on + target segment + all others preserved
        all_segs: list[dict[str, Any]] = [seg_data]
        for seg in current_segments:
            sid = seg.get("id", current_segments.index(seg))
            if sid != segment_id:
                all_segs.append({"id": sid, "on": seg.get("on", False)})

        return await self.set_state({"on": True, "seg": all_segs})

    async def restore_segment(self, segment_id: int, state: dict[str, Any]) -> None:
        """Restore a segment to a previous state.

        Preserves other segments' on/off state (same atomic approach as apply).
        """
        seg_data: dict[str, Any] = {"id": segment_id}

        if "col" in state:
            seg_data["col"] = state["col"]
        if "fx" in state:
            seg_data["fx"] = state["fx"]
        if "sx" in state:
            seg_data["sx"] = state["sx"]
        if "ix" in state:
            seg_data["ix"] = state["ix"]
        if "bri" in state:
            seg_data["bri"] = state["bri"]
        seg_data["on"] = state.get("on", False)

        # Preserve other segments
        current = await self.get_state()
        current_segments = current.get("seg", [])

        all_segs: list[dict[str, Any]] = [seg_data]
        for seg in current_segments:
            sid = seg.get("id", current_segments.index(seg))
            if sid != segment_id:
                all_segs.append({"id": sid, "on": seg.get("on", False)})

        await self.set_state({"seg": all_segs})

    async def _get(self, path: str) -> dict[str, Any]:
        """Make GET request to WLED API."""
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(url, timeout=DEFAULT_TIMEOUT) as resp:
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            _LOGGER.error("Error communicating with WLED at %s: %s", self._host, err)
            raise WLEDApiError(f"Error communicating with WLED: {err}") from err

    async def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """Make POST request to WLED API."""
        url = f"{self._base_url}{path}"
        try:
            async with self._session.post(
                url, json=data, timeout=DEFAULT_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            _LOGGER.error("Error communicating with WLED at %s: %s", self._host, err)
            raise WLEDApiError(f"Error communicating with WLED: {err}") from err


def parse_color(color: str | list[int]) -> list[int]:
    """Parse color from hex string or RGB array."""
    if isinstance(color, list):
        return color[:3]

    if isinstance(color, str):
        color = color.lstrip("#")
        if len(color) == 6:
            return [int(color[i : i + 2], 16) for i in (0, 2, 4)]

    raise ValueError(f"Invalid color format: {color}")
