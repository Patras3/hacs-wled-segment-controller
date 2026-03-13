# WLED Segment Controller

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/Patras3/hacs-wled-segment-controller)](https://github.com/Patras3/hacs-wled-segment-controller/releases)

A Home Assistant custom integration that adds per-segment effect control to WLED devices. This integration **extends** the native WLED integration with segment-level effect management and auto-restore functionality.

## Features

- **Per-segment effect control** - Apply effects to specific segments by name or ID
- **Auto-restore** - Automatically restore previous state after a duration
- **State snapshots** - Save and restore complete WLED states by name
- **Seamless integration** - Works with your existing WLED setup

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu → "Custom repositories"
3. Add `https://github.com/Patras3/hacs-wled-segment-controller` with category "Integration"
4. Search for "WLED Segment Controller" and install
5. Restart Home Assistant
6. Go to Settings → Devices & Services → Add Integration → "WLED Segment Controller"

### Manual

1. Copy `custom_components/wled_segment_controller` to your `config/custom_components/` directory
2. Restart Home Assistant
3. Add the integration via Settings → Devices & Services

## Requirements

- Home Assistant 2024.1.0 or newer
- Native WLED integration configured with your WLED devices

## Services

### `wled_segment_controller.apply_effect`

Apply an effect to a specific WLED segment with optional auto-restore.

```yaml
action: wled_segment_controller.apply_effect
target:
  entity_id: light.wled_living_room
data:
  segment: "Drzwi"        # Segment name or ID
  color: "#FF0000"        # Hex color or [R, G, B]
  effect: "Chase"         # Effect name or ID
  speed: 200              # 0-255
  intensity: 128          # 0-255
  brightness: 255         # 0-255
  duration: 120           # Auto-restore after 120 seconds
```

### `wled_segment_controller.restore_segment`

Manually restore a segment to its state before the last effect was applied.

```yaml
action: wled_segment_controller.restore_segment
target:
  entity_id: light.wled_living_room
data:
  segment: "Drzwi"
```

### `wled_segment_controller.save_state`

Save the current state of all segments as a named snapshot.

```yaml
action: wled_segment_controller.save_state
target:
  entity_id: light.wled_living_room
data:
  name: "evening_ambiance"
```

### `wled_segment_controller.restore_state`

Restore a previously saved state snapshot.

```yaml
action: wled_segment_controller.restore_state
target:
  entity_id: light.wled_living_room
data:
  name: "evening_ambiance"
```

## Example Automations

### Doorbell Flash Effect

Flash a segment red when the doorbell rings, then restore after 30 seconds:

```yaml
automation:
  - alias: "Doorbell Flash"
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell
        to: "on"
    action:
      - action: wled_segment_controller.apply_effect
        target:
          entity_id: light.wled_entrance
        data:
          segment: "Door"
          color: "#FF0000"
          effect: "Strobe"
          speed: 255
          duration: 30
```

### Scene-Based State Management

Save and restore lighting scenes:

```yaml
script:
  save_movie_mode:
    sequence:
      - action: wled_segment_controller.save_state
        target:
          entity_id: light.wled_living_room
        data:
          name: "before_movie"
      - action: wled_segment_controller.apply_effect
        target:
          entity_id: light.wled_living_room
        data:
          segment: 0
          color: "#1a1a2e"
          effect: "Solid"
          brightness: 30

  restore_from_movie:
    sequence:
      - action: wled_segment_controller.restore_state
        target:
          entity_id: light.wled_living_room
        data:
          name: "before_movie"
```

## Segment Identification

Segments can be identified by:

- **Name** - The segment name configured in WLED (e.g., `"Drzwi"`, `"Living Room"`)
- **ID** - The numeric segment ID (e.g., `0`, `1`, `2`)

## How It Works

1. Services receive `entity_id` targeting a WLED light entity
2. Integration resolves the WLED device IP from Home Assistant's device registry
3. Commands are sent directly to WLED's JSON API
4. Auto-restore uses `async_call_later` to schedule state restoration

## Troubleshooting

**Service not available**: Ensure the integration is installed and configured in Settings → Devices & Services.

**Segment not found**: Check that the segment name exactly matches what's configured in WLED, or use the numeric segment ID.

**Effect not found**: Use the exact effect name from WLED's effect list, or use the numeric effect ID.

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

Contributions are welcome! Please open an issue or pull request on [GitHub](https://github.com/Patras3/hacs-wled-segment-controller).
