"""Redacted diagnostics for verifying the reverse-engineered protocols.

Only structure is reported: key names, value types, counts, grid dimensions,
coordinate ranges and fixed error messages. No credentials, tokens, signed
URLs, device identifiers or room names are included.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .k10_api import botslab_region


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    endpoint = coordinator.wonderlab_endpoint
    map_data = coordinator.map_data
    attributes = map_data["attributes"] if map_data else {}
    api = getattr(coordinator, "_k10_api", None)
    data = coordinator.data or {}
    return {
        "device_type": entry.data.get("device_type"),
        "integration_version": "diagnostics-v1",
        "region": {
            "switchbot_host": urlparse(endpoint).hostname if endpoint else None,
            "botslab_region": botslab_region(endpoint),
            "botslab_session": bool(api and api.token),
        },
        "status": {
            "online": data.get("online"),
            "work_status": data.get("work_status"),
            "k10_controls_available": data.get("k10_controls_available"),
            "room_count": len(coordinator.rooms),
        },
        "k10": {
            "last_error": getattr(coordinator, "k10_last_error", None),
            "map_source": getattr(api, "map_source", None),
            "property_types": getattr(coordinator, "k10_property_shapes", {}),
            "map_summary": getattr(coordinator, "k10_map_summary", None),
        },
        "map": {
            "available": map_data is not None,
            "width": attributes.get("width"),
            "height": attributes.get("height"),
            "rooms_drawn": len(attributes.get("rooms") or {}),
            "charger_drawn": attributes.get("charger") is not None,
            "robot_position_available": attributes.get("robot_position_available"),
            "coordinate_fit": attributes.get("coordinate_fit"),
            "source": attributes.get("source"),
        },
    }
