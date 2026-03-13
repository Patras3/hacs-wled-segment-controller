"""Constants for WLED Segment Controller."""

DOMAIN = "wled_segment_controller"

SERVICE_APPLY_EFFECT = "apply_effect"
SERVICE_RESTORE_SEGMENT = "restore_segment"
SERVICE_SAVE_STATE = "save_state"
SERVICE_RESTORE_STATE = "restore_state"

ATTR_SEGMENT = "segment"
ATTR_COLOR = "color"
ATTR_EFFECT = "effect"
ATTR_SPEED = "speed"
ATTR_INTENSITY = "intensity"
ATTR_DURATION = "duration"
ATTR_BRIGHTNESS = "brightness"
ATTR_NAME = "name"

DEFAULT_SPEED = 128
DEFAULT_INTENSITY = 128
DEFAULT_EFFECT = 0  # Solid
