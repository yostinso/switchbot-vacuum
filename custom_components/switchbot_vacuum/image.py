"""Authenticated map image (S10 and K10+) backed by the coordinator's executor cache."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEVICE_TYPE_K10PRO, DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    """Expose maps for S10 and K10+; K10+ Pro has no map protocol yet."""
    if entry.data.get("device_type") != DEVICE_TYPE_K10PRO:
        async_add_entities([SwitchBotMap(hass.data[DOMAIN][entry.entry_id])])


class SwitchBotMap(CoordinatorEntity, ImageEntity):
    """Saved map; station coordinates are never reported as robot position."""

    _attr_content_type = "image/png"
    _attr_name = "Map"
    _attr_icon = "mdi:map"
    _unrecorded_attributes = frozenset({
        "rooms", "charger", "calibration_points", "robot_position", "vacuum_position",
        "charger_position", "coordinate_fit",
    })

    def __init__(self, coordinator):
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._attr_unique_id = f"{coordinator.device_mac}_map"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, coordinator.device_mac)})
        self._attr_image_last_updated = coordinator.map_updated

    @property
    def available(self):
        return self.coordinator.map_data is not None

    @property
    def extra_state_attributes(self):
        return self.coordinator.map_data["attributes"] if self.coordinator.map_data else {}

    @callback
    def _handle_coordinator_update(self):
        self._attr_image_last_updated = self.coordinator.map_updated
        super()._handle_coordinator_update()

    async def async_image(self):
        return self.coordinator.map_data["image"] if self.coordinator.map_data else None
