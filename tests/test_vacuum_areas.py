"""Rooms exposed as Home Assistant vacuum segments for vacuum.clean_area."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant.components.vacuum import Segment, VacuumEntityFeature

from custom_components.switchbot_vacuum import vacuum
from custom_components.switchbot_vacuum.const import (
    DEVICE_TYPE_K10,
    DEVICE_TYPE_K10PRO,
    DEVICE_TYPE_S10,
)


def coordinator(device_type=DEVICE_TYPE_K10, rooms=None):
    c = MagicMock()
    c.entry.data = {"device_type": device_type}
    c.device_mac = "FAKEDEVICE"
    c.device_name = "Robovac"
    c.rooms = {"room1": "Kitchen", "room2": "Den"} if rooms is None else rooms
    c.data = {"firmware": "", "work_status": 7, "rooms": c.rooms}
    c.async_clean_k10_rooms = AsyncMock()
    c.async_send_command = AsyncMock()
    c.async_request_refresh = AsyncMock()
    return c


def mapped(entity, segment_ids):
    entity.registry_entry = SimpleNamespace(id="reg", options={"vacuum": {
        "last_seen_segments": [{"id": s, "name": s, "group": None} for s in segment_ids],
    }})


class SegmentTests(unittest.IsolatedAsyncioTestCase):
    def test_clean_area_feature_except_k10_pro(self):
        for device_type, expected in ((DEVICE_TYPE_K10, True), (DEVICE_TYPE_S10, True),
                                      (DEVICE_TYPE_K10PRO, False)):
            entity = vacuum.SwitchBotS10Vacuum(coordinator(device_type))
            self.assertEqual(
                VacuumEntityFeature.CLEAN_AREA in entity.supported_features, expected, device_type
            )
            self.assertIn(VacuumEntityFeature.START, entity.supported_features)

    async def test_segments_are_rooms(self):
        entity = vacuum.SwitchBotS10Vacuum(coordinator())
        self.assertEqual(await entity.async_get_segments(),
                         [Segment(id="room2", name="Den"), Segment(id="room1", name="Kitchen")])

    async def test_clean_segments_uses_k10_room_protocol_in_order(self):
        c = coordinator()
        entity = vacuum.SwitchBotS10Vacuum(c)
        entity.async_write_ha_state = MagicMock()
        await entity.async_clean_segments(["room2", "room1"])
        c.async_clean_k10_rooms.assert_awaited_once_with(["room2", "room1"], 1, True, None, None)

    async def test_clean_segments_uses_s10_room_ids(self):
        c = coordinator(DEVICE_TYPE_S10, {"ROOM_001": "Kitchen"})
        entity = vacuum.SwitchBotS10Vacuum(c)
        await entity.async_clean_segments(["ROOM_001"])
        payload = c.async_send_command.await_args.args[1]
        self.assertEqual(payload["0"], "clean_rooms")
        self.assertEqual([r["room_id"] for r in payload["1"]["rooms"]], ["ROOM_001"])


class RoomChangeTests(unittest.TestCase):
    def entity(self, rooms=None):
        entity = vacuum.SwitchBotS10Vacuum(coordinator(rooms=rooms))
        entity.async_create_segments_issue = MagicMock()
        return entity

    def test_no_issue_before_areas_are_mapped(self):
        entity = self.entity()
        entity.registry_entry = SimpleNamespace(id="reg", options={})
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_not_called()

    def test_no_issue_when_rooms_match(self):
        entity = self.entity()
        mapped(entity, ["room1", "room2"])
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_not_called()

    def test_issue_once_when_rooms_change(self):
        entity = self.entity({"room1": "Kitchen", "room3": "Office"})
        mapped(entity, ["room1", "room2"])
        entity._async_check_room_changes()
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_called_once()

    def test_no_issue_while_rooms_unknown(self):
        entity = self.entity({})
        mapped(entity, ["room1"])
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_not_called()

    def test_renamed_room_keeps_mapping(self):
        entity = self.entity({"room1": "Galley", "room2": "Den"})
        mapped(entity, ["room1", "room2"])
        entity._async_check_room_changes()
        entity.async_create_segments_issue.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class RoomSensorVisibilityTests(unittest.TestCase):
    def test_new_room_sensors_are_hidden_diagnostics(self):
        from homeassistant.const import EntityCategory

        from custom_components.switchbot_vacuum import sensor

        entity = sensor.SwitchBotRoomSensor(coordinator(), "room1", "Kitchen")
        self.assertFalse(entity.entity_registry_enabled_default)
        self.assertEqual(entity.entity_category, EntityCategory.DIAGNOSTIC)

    def run_migration(self, options):
        from custom_components.switchbot_vacuum import sensor

        def reg(unique_id, disabled_by=None, domain="sensor"):
            return SimpleNamespace(entity_id=f"{domain}.{unique_id}", unique_id=unique_id,
                                   domain=domain, disabled_by=disabled_by)

        entries = [
            reg("FAKEDEVICE_room_room1"),
            reg("FAKEDEVICE_room_room2", disabled_by="user"),
            reg("FAKEDEVICE_battery"),
            reg("FAKEDEVICE_room_x", domain="binary_sensor"),
        ]
        registry = MagicMock()
        hass = MagicMock()
        entry = SimpleNamespace(entry_id="e", data={"device_mac": "FAKEDEVICE"}, options=options)
        with (
            patch.object(sensor.er, "async_get", return_value=registry),
            patch.object(sensor.er, "async_entries_for_config_entry", return_value=entries),
        ):
            sensor._async_hide_existing_room_sensors(hass, entry)
        return registry, hass

    def test_existing_enabled_room_sensors_are_disabled_once(self):
        registry, hass = self.run_migration({"cached_rooms": {}})
        updated = [c.args[0] for c in registry.async_update_entity.call_args_list]
        self.assertEqual(updated, ["sensor.FAKEDEVICE_room_room1"])
        options = hass.config_entries.async_update_entry.call_args.kwargs["options"]
        self.assertEqual(options, {"cached_rooms": {}, "room_sensors_hidden": True})

    def test_migration_does_not_repeat(self):
        registry, hass = self.run_migration({"room_sensors_hidden": True})
        registry.async_update_entity.assert_not_called()
        hass.config_entries.async_update_entry.assert_not_called()
