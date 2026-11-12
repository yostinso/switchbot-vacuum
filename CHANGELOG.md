# Changelog

## 0.6.7

K10+ outside the EU, K10+ maps and Home Assistant area cleaning.

- K10+ room support follows the account's region instead of being EU-only: the SwitchBot regional host selects the Qihoo 360 cloud (`eu1`, `na1`, `ap1`), and map downloads and room uploads are validated against that region's storage.
- K10+ map image: decodes the robot's map package (LZ4 occupancy grid, rooms, no-go zones, dock) with the grid encoding and units detected rather than assumed. Rooms are coloured and labelled near their centres, no-go zones are outlined, unexplored space is transparent, and the image is rotated to match the SwitchBot app.
- Falls back to the permanent recorded map when the robot's one-day map object has expired, and refreshes the map when the robot uploads a new one.
- Live overlay: the robot's reported position and heading plus the cleaning path (`GetCleanTrackWithPosIdx`), polled every 15 s while the robot is cleaning or returning and every 30 s otherwise.
- Map attributes for dashboards: room outlines in pixels and robot millimetres, calibration points and vacuum/charger positions compatible with xiaomi-vacuum-map-card.
- Rooms are exposed as vacuum segments (`VacuumEntityFeature.CLEAN_AREA`, S10 and K10+) so they can be mapped to Home Assistant areas and cleaned with `vacuum.clean_area`; a Repairs issue asks for remapping when rooms are added or removed.
- Room sensors are now diagnostic and disabled by default; existing ones are disabled once on upgrade.
- K10+ diagnostic sensors: Last map refresh (with `last_error`) and Last path update.
- Redacted config-entry diagnostics describing region, map downloads, map structure, room fill and track shape, with no credentials, signed URLs, device identifiers or room names.
- Full suite: 225 passed on Home Assistant 2026.10.4 / Python 3.14 (plus 48 unittest subtests).

## 0.5.0

First release of the maintained cgfm fork.

- Merge the current-HA vacuum battery migration and expose a dedicated battery sensor.
- Renew S10 AWS map credentials using the app policy endpoint, keep SDK/TLS initialization off the event loop, persist discovered rooms, and retain the cache on failures.
- Preserve upstream K10+ controls, button/select platforms and privacy fixes.
- Add the authenticated S10 map image, last completed cleaning route and a timestamped last-received pose marker during active tasks.
- Reject overlays for another map and expire stale pose reports; no claim of live docked-position support.
- Use generic entity naming and retain entity unique IDs; include no personal floorplans, credentials, device identifiers or dashboards.
- Update stale test mocks/status expectations to match existing runtime behavior. Full suite: 162 passed on Home Assistant 2026.10.4 / Python 3.14.7 (plus 48 unittest subtests).
