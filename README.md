# SwitchBot Vacuum for Home Assistant

> **Maintained fork:** This repository is [cgfm/switchbot-vacuum](https://github.com/cgfm/switchbot-vacuum), based on [jaco/switchbot-vacuum](https://github.com/jaco/switchbot-vacuum). Install this fork in HACS to receive this fork's releases. It includes the current-HA battery migration, S10 room-cache/credential repairs, and S10 map/route/position features.
>
> Version 0.5.0 was tested with Home Assistant 2026.10.4 and Python 3.14. Historical routes are supported; the position marker represents the last received pose during active tasks, not a guaranteed live position while docked. No account data, floorplans, robot identifiers or personal dashboards are included.

Custom [HACS](https://hacs.xyz/) integration for SwitchBot robot vacuums (S10 and compatible models) in Home Assistant.

## Why this exists

The official [SwitchBot Cloud integration](https://www.home-assistant.io/integrations/switchbot_cloud/#vacuums) provides basic vacuum controls. This custom integration uses internal cloud APIs, reverse-engineered from the mobile app, to add room-specific cleaning and automatic room name discovery.

## Features

- **Vacuum entity** with full state reporting (cleaning, docked, paused, returning, idle)
- **Error detection (S10)** — an error sensor with specific error types and a problem binary sensor for automations. K10+ error reporting is not implemented; see the model support table below.
- **Room sensor entities** — one per room discovered from the vacuum's map
- **Room-aware cleaning** — clean specific rooms by name or ID. S10 supports mode, suction, water level, passes, and order; K10+ supports vacuum/mop mode, suction, passes, and order. K10+ uses a disposable wipe with no water tank or pump.
- **Automatic room discovery** — room names are downloaded from the vacuum's cloud map every 24 hours
- **Multi-device support** — if your account has multiple SwitchBot vacuums, the config flow lets you pick which one to add
- **Force refresh service** — manually re-download room data and device status on demand

## Model support

Available features depend on the vacuum model and region.

| Capability | S10 | K10+ (EU) |
|------------|-----|-----------|
| Account email/password setup and device selection | Supported | Supported; no manual token capture required |
| Vacuum activity and battery | Supported | Supported |
| Specific error types and problem detection | Supported | Not implemented; do not rely on these sensors for K10+ fault automations |
| Room sensors with names from the map | Supported | Supported |
| `clean_rooms` by name or ID | Supported | Supported |
| Room command mode and suction overrides | Supported | Vacuum or mop; four suction levels |
| Water output | Supported | Not applicable: disposable wipe, no water tank or pump |
| Empty dustbin button | — | Supported while docked |
| Cleaning mode select | — | Vacuum/mop; selecting a mode does not start cleaning |
| Room command passes and order | Supported | One or two passes; optional room order |
| Automatic map refresh every 24 hours | Supported | Supported, plus after the robot uploads a new map |
| Map image with rooms and dock | Supported | Supported (experimental, see below) |
| Robot position on the map | Last received pose during tasks | Reported `CurrentLocation`, redrawn when it changes |
| Multiple vacuums on one account | Supported | Supported |
| `force_refresh` for status and rooms | Supported | Supported |

K10+ room cleaning and maps follow the account's SwitchBot region: EU (`eu1`), US (`na1`) and Asia-Pacific (`ap1`). Only EU has been verified on hardware so far. K10+ Pro room cleaning is not supported.

### Tested setup

K10+ support was tested with a **SwitchBot Mini Robot Vacuum K10+ (EU)** in **Home Assistant Container (Docker) 2026.10.1**. The installation included the battery compatibility patch now integrated in this fork.

Verified through Home Assistant: email/password login and reauthentication after a password change, room discovery, device status and battery, starting a selected room with one pass, pausing, changing suction from max to quiet, quiet room cleaning, and emptying the dustbin.

A **SwitchBot Mini Robot Vacuum K10+ on a US account** (`na1`) was tested with **Home Assistant OS 2026.11.1**. Verified: region detection, room discovery, the map image (rooms, labels, no-go zones, dock, orientation), the robot position and cleaning path updating during a clean, and starting a selected room.

Mop mode, two passes, multi-room cleaning order, multiple physical vacuums, and long-running automatic refresh have not yet been verified on hardware. For the US setup, `vacuum.clean_area`, the post-cleaning map refresh and the diagnostic sensors have not yet been verified on hardware. The Asia-Pacific region (`ap1`) and K10+ Pro have not been tested.

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu → **Custom repositories**
3. Add `https://github.com/cgfm/switchbot-vacuum` with category **Integration**
4. Search for "SwitchBot Vacuum" and install
5. Restart Home Assistant

### Manual

Copy `custom_components/switchbot_vacuum/` to your Home Assistant `config/custom_components/` directory and restart.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **SwitchBot Vacuum**
3. Enter your SwitchBot account email and password
4. If multiple vacuums are found, select the one to add

You can add the integration multiple times for multiple vacuums.

If you change your SwitchBot password, open the integration entry menu and select **Reconfigure** to enter the current password. Authentication failures also offer a reauthentication form. Both flows keep the existing device and entities, and check that the same vacuum is still present in the account.

## Screenshots

| Device & room entities | Clean rooms service | Room sensor detail |
|---|---|---|
| ![Device entities](screenshots/device-entities.png) | ![Clean rooms service](screenshots/clean-rooms-service.png) | ![Room sensor detail](screenshots/room-sensor-detail.png) |

## Entities

### Vacuum

The main entity (`vacuum.switchbot_vacuum`) provides:

| Feature | Description |
|---------|-------------|
| **State** | idle, cleaning, docked, paused, returning |
| **Battery** | A separate battery sensor on the same device |
| **Fan speed** | quiet, standard, strong, max |
| **Start** | Start full cleaning |
| **Stop / Pause** | Stop or pause current cleaning |
| **Return to base** | Send vacuum to charging station |

#### Extra attributes

The vacuum entity exposes additional attributes you can use in automations and templates. Water and cleaning-summary attributes below describe S10. K10+ exposes the actual `clean_type` (`sweep` or `mop`), `mopping_pad_installed`, rooms and a default pass count, not the last room command's pass count:

| Attribute | Example | Description |
|-----------|---------|-------------|
| `water_level` | `1` | Current water output level (1–3) |
| `clean_type` | `sweep_mop` | Current mode: `sweep`, `mop`, or `sweep_mop` |
| `times` | `1` | Number of cleaning passes |
| `last_clean_area` | `45` | Area cleaned in last session (m²) |
| `last_clean_time` | `30` | Duration of last session (minutes) |
| `rooms` | `{"ROOM_013": "Kitchen", ...}` | All known rooms with IDs and names |

**Reading attributes in templates:**

```yaml
# Get battery level
{{ states('sensor.your_vacuum_battery') }}  {# Replace with your actual battery entity ID #}

# Get current clean type
{{ state_attr('vacuum.switchbot_vacuum', 'clean_type') }}

# Get last cleaned area
{{ state_attr('vacuum.switchbot_vacuum', 'last_clean_area') }} m²

# List all room names
{% for id, name in state_attr('vacuum.switchbot_vacuum', 'rooms').items() %}
  {{ name }} ({{ id }})
{% endfor %}
```

### Error & problem detection

| Entity | Type | Description |
|--------|------|-------------|
| `binary_sensor.*.problem` | Problem | ON when any error is active — simplest automation trigger |
| `sensor.*.error` | Sensor | Current error type as a string (e.g. `stuck`, `clean_water_tank_empty`) or `none` |

The following mappings apply to S10. K10+ currently supplies a fixed zero error code, so its error/problem entities do not provide reliable fault detection.

The S10 error sensor state is one of:

| State | Error code | Description |
|-------|-----------|-------------|
| `none` | 0 | No error |
| `stuck` | 2000 | Robot is stuck |
| `wheel_stuck` | 2001 | Wheel stuck or suspended |
| `side_brush_stuck` | 2002 | Side brush tangled |
| `main_brush_stuck` | 2003 | Main brush/roller tangled |
| `bumper_stuck` | 2004 | Bumper stuck — check for debris |
| `dust_bin_missing` | 2005 | Dust bin not installed |
| `filter_clogged` | 2006 | Filter needs cleaning |
| `cliff_sensor_error` | 2007 | Cliff/drop sensor error |
| `low_battery` | 2008 | Battery too low to continue |
| `charging_error` | 2009 | Cannot charge — check dock contacts |
| `internal_error` | 2010 | Internal system error |
| `laser_sensor_error` | 2011 | LDS/laser sensor error |
| `path_blocked` | 2012 | Navigation error — cannot find path |
| `clean_water_tank_empty` | 2728 | Clean water tank empty |
| `dirty_water_tank_full` | 2739 | Dirty water tank full |
| `dirty_water_tank_removed` | 2740 | Dirty water tank removed |
| `fault` | — | Work status is fault but no specific error code |
| `error_XXXX` | XXXX | Unknown code (logged as warning for discovery) |

Both entities expose `error_code` (raw integer) and `error_type`/`error_description` as extra attributes.

### Room sensors

Each room discovered from the vacuum's map gets a sensor entity (e.g. `sensor.kitchen`, `sensor.bedroom`). These are diagnostic entities, **disabled by default**, because rooms are already available for area cleaning, in the vacuum's `rooms` attribute and on the map. Room sensors created by earlier versions are disabled once on upgrade; enable any you use from the device page and they stay enabled. The sensor value is the room name, and the `room_id` attribute contains the internal ID (S10: e.g. `ROOM_013`; K10+: e.g. `room1`).

```yaml
# Get room ID for use in clean_rooms service
{{ state_attr('sensor.kitchen', 'room_id') }}
```

## Services

### `vacuum.clean_area` (Home Assistant areas)

On Home Assistant 2026.x the vacuum entity reports its rooms as cleanable segments (S10 and K10+; not K10+ Pro). In the vacuum entity's settings, map each room to one of your Home Assistant areas. Then use the built-in action, voice assistants, or the standard vacuum UI:

```yaml
action: vacuum.clean_area
target:
  entity_id: vacuum.switchbot_vacuum
data:
  cleaning_area_id:
    - kitchen
    - dining_room
```

Rooms are cleaned in the order given, with each room's app settings. If rooms are later added or removed in the SwitchBot app, a Repairs issue asks you to update the mapping. Renaming a room keeps its mapping. Use `switchbot_vacuum.clean_rooms` below when you need per-run mode, suction or pass overrides.

### `switchbot_vacuum.clean_rooms`

Clean specific rooms by name or ID. The example and mode/suction/water defaults below apply to S10. For K10+, the same service also accepts `mode` (`sweep` or `mop`) and `fan_level` (1–4), as shown in the K10+ section. Water settings and simultaneous sweep/mop do not apply to this hardware.

```yaml
service: switchbot_vacuum.clean_rooms
target:
  entity_id: vacuum.switchbot_vacuum
data:
  rooms:
    - "Kitchen"
    - "Bedroom"
    - "ROOM_003"
  mode: "mop"
  fan_level: 2
  water_level: 2
  times: 1
  force_order: true
```

| Parameter | Required | Default | Values |
|-----------|----------|---------|--------|
| `rooms` | yes | — | List of room names or IDs (can mix) |
| `mode` | no | S10: `sweep_mop`; K10+: current mode | `sweep`, `mop`; `sweep_mop` is S10 only |
| `fan_level` | no | S10: `1`; K10+: each room's app setting | 1 (quiet), 2 (standard), 3 (strong), 4 (max) |
| `water_level` | no | S10: `1` | S10 only: 1 (low), 2 (medium), 3 (high); omit for K10+ |
| `times` | no | `1` | 1 or 2 passes |
| `force_order` | no | `true` | Clean rooms in the specified order |

### `switchbot_vacuum.force_refresh`

Force an immediate refresh of device status and room data from the vacuum's cloud map.

```yaml
service: switchbot_vacuum.force_refresh
target:
  entity_id: vacuum.switchbot_vacuum
```

## Automation examples

### Mop the kitchen every day at 10:00

```yaml
automation:
  - alias: "Daily kitchen mop"
    trigger:
      - platform: time
        at: "10:00:00"
    action:
      - service: switchbot_vacuum.clean_rooms
        target:
          entity_id: vacuum.switchbot_vacuum
        data:
          rooms:
            - "Kitchen"
          mode: "mop"
          water_level: 2
```

### Notify when water tank is empty

```yaml
automation:
  - alias: "S10 water tank empty"
    trigger:
      - platform: state
        entity_id: sensor.floor_cleaning_robot_s10_error
        to: "clean_water_tank_empty"
    action:
      - service: notify.mobile_app
        data:
          title: "SwitchBot S10"
          message: "Clean water tank is empty — refill to continue cleaning"
```

### Notify on any problem

```yaml
automation:
  - alias: "S10 any problem"
    trigger:
      - platform: state
        entity_id: binary_sensor.floor_cleaning_robot_s10_problem
        to: "on"
    action:
      - service: notify.mobile_app
        data:
          title: "SwitchBot S10 problem"
          message: >
            Error: {{ state_attr('sensor.floor_cleaning_robot_s10_error', 'error_description') }}
            (code {{ state_attr('sensor.floor_cleaning_robot_s10_error', 'error_code') }})
```

### Full clean when everyone leaves

```yaml
automation:
  - alias: "Clean when away"
    trigger:
      - platform: state
        entity_id: group.family
        to: "not_home"
    action:
      - service: vacuum.start
        target:
          entity_id: vacuum.switchbot_vacuum
```

## K10+ rooms

Room names and numeric IDs are read from the vacuum's current cloud map. Creating cleaning schedules is not required. Use `switchbot_vacuum.force_refresh` to refresh the map and room sensors.

Use the existing `switchbot_vacuum.clean_rooms` service for K10+ room cleaning:

```yaml
action: switchbot_vacuum.clean_rooms
target:
  entity_id: vacuum.switchbot_vacuum
data:
  rooms:
    - "Entry"
  mode: sweep
  fan_level: 1
  times: 1
  force_order: true
```

The action accepts room names, numeric IDs as strings, and legacy IDs such as `room1`. It supports one or two passes. Omit `fan_level` to keep each room's app settings, or supply 1–4 to override only the selected rooms for this request. Omit `mode` to keep the current device mode, or choose `sweep` / `mop`. Unknown rooms, `water_level`, and `sweep_mop` are rejected. A failed K10+ room command never falls back to whole-house cleaning.

### K10+ controls

The device also exposes a **Cleaning mode** select (`vacuum`, `mop`) and an **Empty dustbin** button. Entity IDs are generated by Home Assistant and can be found on the device page.

- Install the mop plate with its cleaning sheet before selecting `mop`; remove it before selecting `vacuum`. K10+ cannot vacuum and mop simultaneously. [SwitchBot's hardware guidance](https://support.switch-bot.com/hc/en-us/articles/14215810297879-Can-SwitchBot-Mini-Robot-Vacuum-K10-K10-Pro-K11-K11-Pro-Simultaneously-Vacuum-and-Mop).
- Changing the select sets the mode without starting cleaning. Pause an active task before changing modes. Room cleaning waits for the new mode to be confirmed before starting.
- Use the standard `vacuum.set_fan_speed` action with `quiet`, `standard`, `strong`, or `max` to set device suction. Room commands have their own `fan_level` override; when omitted, app room settings are retained.
- Press **Empty dustbin** while docked to collect dust into the base. The command is sent once and the robot's reply is checked.
- Mode and mop-plate state are refreshed with each status update. If that read fails, these extra controls become unavailable while basic vacuum status can continue through SwitchBot.

Start requests are never retried automatically. If confirmation times out, check the robot before retrying.

K10+ controls require an internet connection and use an unofficial cloud API, which may change.

### K10+ regions

The K10+ is a Qihoo 360 platform robot. Its rooms and map live on 360's regional cloud (`{region}-sapp-api.botslab.com`), which is chosen from the SwitchBot account's regional host: `wonderlabs.eu…` → `eu1`, `wonderlabs.us…` → `na1`, `wonderlabs.ap…` → `ap1`. Signed map downloads are only followed to that region's 360 storage buckets, and room uploads only go to that region's Aliyun OSS bucket. If a region uses a different upload provider, room cleaning fails with an error naming the provider, and no cleaning is started.

### K10+ map (experimental)

The **Map** image entity is now created for K10+ as well. It decodes the map package the robot uploads after mapping or cleaning: an LZ4-compressed occupancy grid, the room polygons used for room cleaning, no-go areas and the dock. Rooms are filled in their own colours and labelled with their names near the middle, no-go zones are drawn in red, and walls and obstacles are dark. Unexplored space is transparent, so the image works on light and dark dashboards.

- If the current map object has expired (the robot stores it for one day), the permanent recorded map for the same map ID is used instead.
- The robot's reported `CurrentLocation` is drawn as an orange marker with a heading line whenever it changes. Heading units have not been confirmed on hardware.
- The last cleaning path (from the robot's `GetCleanTrackWithPosIdx` service) is drawn in green. It is loaded once per map and followed incrementally while the robot is cleaning, paused or returning. While the robot is cleaning or returning, the integration polls every 15 seconds instead of 30. Gaps over 1 m are not joined.
- The image is rotated 180° from the raw grid to match the SwitchBot app.
- Two diagnostic sensors show freshness. **Last map refresh** is when Home Assistant last fetched and rendered the map, with the last failure in its `last_error` attribute. **Last path update** is when new cleaning-path points last arrived; after a restart it shows when the path was first re-fetched.
- Attributes: `rooms` (pixel `outline`, `outline_vacuum` in robot millimetres, label `x`/`y`), `charger`, `charger_position`, `vacuum_position`, `calibration_points`, and `coordinate_fit`.
- `calibration_points` and `vacuum_position` follow the conventions of [xiaomi-vacuum-map-card](https://github.com/PiotrMachowski/lovelace-xiaomi-vacuum-map-card), so the image can be used as that card's map with `calibration_source: {entity: image.<your_map>}`. Use `outline_vacuum` for room outlines in its `predefined_selections`, and call `switchbot_vacuum.clean_rooms` from its service templates.

The grid encoding is detected rather than assumed: on a K10+ unexplored space is 127, floor 255 and walls 0, while other 360 robots use 0, 127 and 255. Units are also checked rather than assumed: the decoder picks the interpretation that puts the room polygons on the grid, and reports it in `coordinate_fit`. If the map looks wrong, use **Download diagnostics** on the integration. The file contains only structure (key names, types, grid size, value counts and coordinate ranges), with no credentials, signed URLs, device identifiers or room names. Attach it to an issue.

## Technical notes

- The integration polls the device every 30 seconds
- Auth tokens are refreshed automatically every 1.5 hours
- Room names are refreshed from the cloud map every 24 hours (or on demand via `force_refresh`); room refresh runs as a background task so it never blocks setup
- For S10, error codes are read from device property 1019 and mapped to descriptive strings; unknown codes are logged as warnings so they can be reported and added
- Error code mappings were reverse-engineered from the SwitchBot APK's Flutter module (`feature_sweeper`, Qihoo 360 SDK)

## License

MIT

## S10 map, historical route and position

An authenticated `image` entity displays the saved cloud map, room boundaries and the latest completed cleaning task's route. The same device-map unique ID is retained when updating an existing installation; existing entity IDs are therefore preserved. New installations use the generic name **Map**. Select the actual image entity in Home Assistant rather than assuming a fixed entity ID.

- Green route lines are cleaning movements; grey lines are other recorded movements. Segments are kept separate and reports from another map UUID are not overlaid.
- The blue marker is the stored charging station. It is never used as a substitute for the robot's position.
- An orange marker and heading show the last received robot pose during an active task, polled at the coordinator's approximately 30-second interval. Unchanged pose reports expire after 60 seconds; the marker is removed and monitoring is closed when the robot becomes idle/docked.
- A reliable continuously updated position while docked has **not** been demonstrated. Old points remaining in the cloud feed do not establish a fresh position.
- Map data is refreshed on startup and daily, or with `switchbot_vacuum.force_refresh`. The latest completed cleaning report is checked about once a minute and downloaded only when the task changes. A stopped test task may therefore become the newest historical route.
- The `rooms` attribute provides pixel-space room outlines. Existing map-card/dashboard configuration can use these outlines; this repository contains no installation-specific room-selection script or dashboard.

SDK initialization, TLS loading, archive parsing and PNG generation run in executor workers. Credentials are requested on demand and never stored in the room cache.

## Fork development and release policy

HACS follows releases from **cgfm/switchbot-vacuum**. Upstream changes are not automatically installed or merged into this fork. Reviewed changes are published as explicit release tags. The focused upstream repair branches remain independent from the fork's map/route extensions.

For the isolated test environment used for 0.5.0:

```sh
python -m venv .venv
.venv/bin/pip install homeassistant==2026.10.4 aiobotocore pytest pytest-asyncio
.venv/bin/python -m pytest -q
```

Tests use synthetic maps, fake HTTP responses and fake robot services; they do not require a SwitchBot account or initiate cleaning.
