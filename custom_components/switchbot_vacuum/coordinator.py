"""Data update coordinator for SwitchBot Vacuum."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_AUTH_HOST,
    API_TIMEOUT,
    APP_VERSION,
    CLIENT_ID,
    CONF_DEVICE_MAC,
    CONF_PASSWORD,
    CONF_PRODUCT_KEY,
    CONF_USERNAME,
    DEVICE_TYPE_K10,
    DEVICE_TYPE_K10PRO,
    DOMAIN,
    K10_WORK_STATUS_STANDBY,
    K10PRO_PROP_AUTO_RESTART,
    K10PRO_PROP_BATTERY,
    K10PRO_PROP_CHILD_LOCK,
    K10PRO_PROP_DUST_COLECT_FREQUENCY,
    K10PRO_PROP_DUST_COLECT_TIME,
    K10PRO_PROP_ONLINE,
    K10PRO_PROP_SUCTION_POW_LEVEL,
    K10PRO_PROP_WORK_STATUS,
    PROP_BATTERY,
    PROP_CLEAN_MODE,
    PROP_CLEAN_SUMMARY,
    PROP_ERROR_CODE,
    PROP_FIRMWARE,
    PROP_MAP_INFO,
    PROP_ONLINE,
    PROP_ROOM_PLANS,
    PROP_S3_BUCKET,
    PROP_WORK_STATUS,
    S3_REGION,
    SUPPORTED_DEVICE_TYPES,
    TOKEN_REFRESH_SECONDS,
    UPDATE_INTERVAL_SECONDS,
)
from .k10_api import CLEAN_MODES, K10Api, K10ProtocolError, botslab_region, room_names
from .k10_map import decode_k10_map, describe_map, parse_path, parse_pose, render_k10_overlay

# Cleaning, cleaning variants, paused and returning to the dock (const.py K10 statuses).
K10_TRACK_ACTIVE_STATUSES = frozenset({1, 2, 3, 4, 5})
# Cleaning and returning to the dock (not paused): poll faster to follow the path.
K10_MOVING_STATUSES = frozenset({1, 2, 3, 5})
K10_ACTIVE_UPDATE_SECONDS = 15
from .route_data import download_route, render_position

_LOGGER = logging.getLogger(__name__)


def _restore_rooms(hass: HomeAssistant, entry: ConfigEntry | None) -> dict[str, str]:
    """Restore the room cache, migrating previously discovered room entities."""
    if entry is None:
        return {}
    cached = entry.options.get("cached_rooms")
    if isinstance(cached, dict):
        return {
            room_id: name for room_id, name in cached.items()
            if isinstance(room_id, str) and isinstance(name, str)
        }

    # Upgrade recovery only: these unique IDs and original names were created
    # from a successful cloud map download. Never use names of other devices.
    prefix = f"{entry.data.get(CONF_DEVICE_MAC)}_room_"
    registry = er.async_get(hass)
    rooms = {}
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.platform == DOMAIN
            and entity.domain == "sensor"
            and entity.unique_id.startswith(prefix)
            and entity.original_name
        ):
            room_id = entity.unique_id[len(prefix):]
            if room_id:
                rooms[room_id] = entity.original_name
    return rooms


def _download_map_rooms(
    creds: dict[str, Any], bucket: str, resource: str, region: str = S3_REGION
) -> dict[str, Any] | None:
    """Download and decode an S10 map entirely in an executor thread.

    aiobotocore lazily reads service models and certificates even during async
    requests. Give this self-contained operation a private loop in the worker;
    no SDK session, client, response or task crosses the thread boundary.
    """
    import aiobotocore.session
    from aiobotocore.config import AioConfig

    async def download() -> bytes:
        session = aiobotocore.session.get_session()
        async with session.create_client(
            "s3",
            region_name=region,
            aws_access_key_id=creds["accessKeyId"],
            aws_secret_access_key=creds["secretAccessKey"],
            aws_session_token=creds["sessionToken"],
            config=AioConfig(
                connect_timeout=10,
                read_timeout=30,
                retries={"total_max_attempts": 2, "mode": "standard"},
            ),
        ) as s3:
            response = await s3.get_object(Bucket=bucket, Key=resource)
            async with response["Body"] as body:
                return await body.read()

    zip_bytes = asyncio.run(download())

    from .map_data import decode_map

    return decode_map(zip_bytes)


def _render_k10_map(
    map_data: dict[str, Any], record_count: int, source: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Decode a K10+ map in an executor; always return a redacted summary."""
    summary = describe_map(map_data, record_count) | {"source": source}
    try:
        rendered = decode_k10_map(map_data)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        summary["render_error"] = type(exc).__name__
        _LOGGER.warning("K10+ map could not be rendered (%s)", type(exc).__name__)
        return None, summary
    summary["coordinate_fit"] = rendered["attributes"]["coordinate_fit"]
    summary["room_fill"] = rendered.get("_room_stats")
    return rendered, summary


STATUS_PROPS = [PROP_ONLINE, PROP_BATTERY, PROP_WORK_STATUS, PROP_ERROR_CODE,
                PROP_CLEAN_MODE, PROP_CLEAN_SUMMARY, PROP_FIRMWARE]
K10PRO_STATUS_PROPS = [
    K10PRO_PROP_ONLINE,
    K10PRO_PROP_BATTERY,
    K10PRO_PROP_SUCTION_POW_LEVEL,
    K10PRO_PROP_WORK_STATUS,
    K10PRO_PROP_DUST_COLECT_FREQUENCY,
    K10PRO_PROP_CHILD_LOCK,
    K10PRO_PROP_DUST_COLECT_TIME,
    K10PRO_PROP_AUTO_RESTART,
]


def _status_code_for_log(value: Any) -> int | str:
    """Keep numeric error codes without exposing other cloud response data."""
    return value if type(value) is int else "unknown"


class SwitchBotS10Coordinator(DataUpdateCoordinator):
    """Manage fetching data from SwitchBot Vacuum API."""

    # K10+ map state; class defaults so partially constructed instances stay valid.
    # Each is reassigned (never mutated in place).
    _k10_pose: tuple[float, float, float] | None = None
    _k10_map_marker: Any = None
    k10_map_summary: dict[str, Any] | None = None
    k10_property_shapes: dict[str, str] = {}
    k10_last_error: str | None = None
    k10_track_error: str | None = None
    _k10_path: list[tuple[float, float]] = []
    _k10_path_drawn = -1
    _k10_track_index = 0
    _k10_track_loaded = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.entry = entry
        self.access_token: str | None = None
        self.bot_region: str | None = None
        self.wonderlab_endpoint: str | None = None
        self.device_mac: str | None = None
        self.device_name: str | None = None
        self.user_id: str | None = None
        self._uuid: str = str(uuid.uuid4())
        self._token_expiry: float = 0
        self._rooms: dict[str, str] = _restore_rooms(hass, entry)
        self.map_data: dict[str, Any] | None = None
        self.map_updated: datetime | None = None
        self._last_route_attempt: float = 0
        self._route_refresh_lock = asyncio.Lock()
        self._map_bucket: str | None = None
        self._map_monitor_open = False
        self._position_key: tuple | None = None
        self._position_received_at: float = 0
        self._position_task: str | None = None
        self._position_index = 0
        self._route_task: asyncio.Task | None = None
        self._map_stopping = False
        self._last_room_refresh: float = 0
        self._last_room_attempt: float | None = None
        self._k10_api: K10Api | None = None
        self._k10_group_id: str | None = None

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )

    def _is_k10(self) -> bool:
        """Return True if device is K10+."""
        return self.entry.data.get("device_type") == DEVICE_TYPE_K10

    def _is_k10_pro(self) -> bool:
        """Return True if device is K10+."""
        return self.entry.data.get("device_type") == DEVICE_TYPE_K10PRO

    def _headers(self, auth: str | None = None) -> dict[str, str]:
        """Build common request headers."""
        return {
            "authorization": auth if auth is not None else (self.access_token or ""),
            "uuid": self._uuid,
            "requestid": str(uuid.uuid4()),
            "appversion": APP_VERSION,
            "content-type": "application/json; charset=UTF-8",
        }

    async def async_login(self) -> None:
        """Authenticate with SwitchBot API."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{API_AUTH_HOST}/account/api/v1/user/login",
                headers=self._headers(auth=""),
                json={
                    "clientId": CLIENT_ID,
                    "deviceInfo": {
                        "deviceId": self._uuid,
                        "deviceName": "Home Assistant",
                        "model": "Home Assistant",
                    },
                    "grantType": "password",
                    "password": self.entry.data[CONF_PASSWORD],
                    "username": self.entry.data[CONF_USERNAME],
                    "verifyCode": "",
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                body = data.get("body", {})
                token = body.get("access_token")
                if not token:
                    raise ConfigEntryAuthFailed(
                        f"Login failed: {data.get('message', data.get('statusCode', 'unknown'))}"
                    )
                self.access_token = token
                self._token_expiry = time.time() + TOKEN_REFRESH_SECONDS
            async with session.post(
                f"{API_AUTH_HOST}/account/api/v1/user/userinfo",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                body = data.get("body", {})
                self.bot_region = body.get("botRegion")
                if not self.bot_region:
                    raise ConfigEntryAuthFailed(
                        f"Get UserInfo failed: {data.get('message', data.get('statusCode', 'unknown'))}"
                    )
            async with session.post(
                f"{API_AUTH_HOST}/admin/admin/api/v1/botregion/endpoint",
                headers=self._headers(),
                json={
                    "botRegion": self.bot_region,
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                body = data.get("data", [])
                self.wonderlab_endpoint = next(
                    (v["host"] for v in body if v["name"] == "wonderlabs"), None
                )
                if not self.wonderlab_endpoint:
                    raise ConfigEntryAuthFailed(
                        f"Get Endpoints failed: {data.get('message', data.get('resultCode', 'unknown'))}"
                    )

    async def async_discover_devices(self) -> list[dict[str, Any]]:
        """Find all supported vacuum devices in the account."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/wonder/device/v3/getdevice",
                headers=self._headers(),
                json={"required_type": "All"},
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                devices = []
                for device in data.get("body", {}).get("Items", []):
                    device_type = device.get("device_detail", {}).get("device_type")
                    if device_type in SUPPORTED_DEVICE_TYPES:
                        devices.append({
                            "device_mac": device["device_mac"],
                            "device_name": device.get("device_name", "SwitchBot Vacuum"),
                            "device_type": device_type,
                            "product_key": device.get("product_key", ""),
                            "user_id": device.get("userID"),
                            "group_id": device.get("groupID"),
                        })
                return devices

    def set_device(self, device_mac: str, device_name: str, user_id: str | None = None) -> None:
        """Set the target device after config flow discovery."""
        self.device_mac = device_mac
        self.device_name = device_name
        self.user_id = user_id

    async def async_get_properties(self, property_ids: list[int]) -> dict[int, Any]:
        """Fetch device properties from shadow API (S10 only)."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/device/device/v1/shadow/getByIDs",
                headers=self._headers(),
                json={"deviceID": self.device_mac, "propertyIDs": property_ids},
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                if data.get("resultCode") != 100:
                    code = _status_code_for_log(data.get("resultCode"))
                    raise UpdateFailed(f"Property fetch failed (resultCode {code})")
                result = {}
                for pid_str, prop in (data.get("data") or {}).items():
                    result[int(pid_str)] = prop.get("value")
                return result

    async def async_send_command(
        self, function_id: int, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a command to the device via invokeFunc (S10 only)."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/command/cmd/api/v1/func/invoke",
                headers=self._headers(),
                json={
                    "deviceID": self.device_mac,
                    "functionID": function_id,
                    "params": params,
                    "notify": {
                        "type": "mqtt",
                        "url": f"v1_1/{self._uuid}/APP_HA_{self._uuid}/funcResp",
                    },
                    "optSrc": "app",
                    "timeout": 65535,
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                return await resp.json()

    async def _get_product_key(self) -> str:
        """Return product_key from config entry or re-discover it."""
        key = self.entry.data.get(CONF_PRODUCT_KEY, "")
        if key:
            return key
        _LOGGER.info("product_key missing from config, re-discovering devices")
        devices = await self.async_discover_devices()
        for device in devices:
            if device["device_mac"] == self.device_mac:
                key = device.get("product_key", "")
                if key:
                    _LOGGER.info("Found product_key for %s: %s", self.device_mac, key)
                break
        return key

    async def async_send_action(
        self, identifier: str, input_data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a command to K10+ via setAction."""
        product_key = await self._get_product_key()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/wonder/sweeper360/v1/device/setAction",
                headers=self._headers(),
                json={
                    "productKey": product_key,
                    "deviceName": self.device_mac,
                    "identifier": identifier,
                    "input": input_data or {},
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                return await resp.json()

    async def async_send_info(self, items: dict[str, Any]) -> dict[str, Any]:
        """Set K10+ device properties via setInfo endpoint."""
        product_key = await self._get_product_key()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/wonder/sweeper360/v1/device/setInfo",
                headers=self._headers(),
                json={
                    "productKey": product_key,
                    "deviceName": self.device_mac,
                    "items": items,
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                return await resp.json()

    async def async_get_k10_status(self) -> dict[str, Any]:
        """Fetch real-time K10+ status via getstatus endpoint."""
        session = async_get_clientsession(self.hass)
        async with session.post(
            f"{self.wonderlab_endpoint}/wonder/devicestatus/v1/getstatus",
            headers=self._headers(),
            json={"items": [self.device_mac]},
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        ) as resp:
            data = await resp.json()
            if data.get("statusCode") != 100:
                code = _status_code_for_log(data.get("statusCode"))
                raise UpdateFailed(f"K10+ getstatus failed (statusCode {code})")
            items = data.get("body", {}).get("items", [])
            if not items:
                raise UpdateFailed("K10+ getstatus returned no items")
            return items[0]

    async def _authorize_k10_api(self, session: aiohttp.ClientSession) -> K10Api:
        """Resolve the selected device's family and delegated Qihoo session."""
        await self._ensure_token()
        if not self._is_k10() or not self.device_mac:
            raise K10ProtocolError("Select a K10+ device before using K10 room support")
        if not self._k10_group_id:
            devices = await self.async_discover_devices()
            selected = next((d for d in devices if d["device_mac"] == self.device_mac), None)
            if not selected or not selected.get("group_id"):
                raise K10ProtocolError("The selected K10+ has no family identifier")
            self._k10_group_id = selected["group_id"]
        if self._k10_api is None:
            self._k10_api = K10Api(
                self.device_mac, await self._get_product_key(), self._uuid.replace("-", "")
            )
        if not self.access_token or not self.wonderlab_endpoint:
            raise K10ProtocolError("SwitchBot account session is unavailable")
        await self._k10_api.authorize(
            session, self.wonderlab_endpoint, self.access_token, self._k10_group_id
        )
        return self._k10_api

    async def async_clean_k10_rooms(
        self, rooms: list[str], times: int, force_order: bool,
        mode: str | None = None, fan_level: int | None = None,
    ) -> None:
        """Start only validated rooms, preserving their configured suction."""
        session = async_get_clientsession(self.hass)
        api = await self._authorize_k10_api(session)
        await api.clean_rooms(session, rooms, times, force_order, mode=mode, fan_level=fan_level)

    async def async_k10_control(self, command: str, value: str | int | None = None) -> None:
        """Execute a supported K10+ control without leaking signed network URLs."""
        if command not in ("mode", "suction", "empty_dustbin"):
            raise HomeAssistantError("Unknown K10+ control")
        try:
            session = async_get_clientsession(self.hass)
            api = await self._authorize_k10_api(session)
            if command == "mode":
                await api.set_mode(session, value)
            elif command == "suction":
                await api.set_suction(session, value)
            else:
                await api.empty_dustbin(session)
        except K10ProtocolError as exc:
            raise HomeAssistantError(str(exc)) from None
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            raise HomeAssistantError(
                f"K10+ control failed ({type(exc).__name__}); check robot state before retrying"
            ) from None
        await self.async_request_refresh()

    async def async_refresh_k10_rooms(self) -> None:
        """Read K10+ map rooms; retain schedule discovery for K10+ Pro."""
        if self._is_k10():
            try:
                session = async_get_clientsession(self.hass)
                api = await self._authorize_k10_api(session)
                map_data, _ = await api.get_map(session, strict=False)
                if "smartArea" in map_data:
                    self._rooms = {
                        f"room{room_id}": name for room_id, name in room_names(map_data).items()
                    }
                    self._persist_rooms()
                self._last_room_refresh = time.time()
                self.k10_last_error = None
            except K10ProtocolError as exc:
                # Protocol errors carry only fixed messages and numeric codes.
                self.k10_last_error = str(exc)
                _LOGGER.warning("K10+ map refresh failed: %s", exc)
                return
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                # Network exceptions may contain a signed map URL; log only the type.
                self.k10_last_error = type(exc).__name__
                _LOGGER.warning("K10+ map refresh failed (%s)", type(exc).__name__)
                return
            rendered, self.k10_map_summary = await self.hass.async_add_executor_job(
                _render_k10_map, map_data, api.map_record_count, api.map_source
            )
            if rendered is not None:
                self.map_data = rendered
                self.map_updated = datetime.now().astimezone()
                self._k10_pose, self._k10_path_drawn = None, -1
                self._k10_track_loaded = False  # Reload the track for the new map.
                _LOGGER.info(
                    "Rendered K10+ map (%s rooms, fit %s)", len(rendered["rooms"]),
                    rendered["attributes"]["coordinate_fit"],
                )
            return
        try:
            resp = await self.async_send_action("GetCleanPolicyList")
            if resp.get("statusCode") != 100:
                _LOGGER.warning(
                    "GetCleanPolicyList failed (statusCode %s)",
                    _status_code_for_log(resp.get("statusCode")),
                )
                return

            result = json.loads(resp["body"]["result"])
            policy = json.loads(result["CleanPolicyList"])

            room_ids: set[int] = set()
            for entry in policy.get("value", []):
                for rid in entry.get("smartAreaIds", []):
                    room_ids.add(rid)

            if room_ids:
                self._rooms = {f"room{rid}": f"room{rid}" for rid in sorted(room_ids)}
                self._last_room_refresh = time.time()
                _LOGGER.info("Loaded %d K10+ rooms: %s", len(self._rooms), list(self._rooms))
        except Exception as exc:
            _LOGGER.warning("Failed to refresh K10+ rooms: %s", exc)

    def _extract_rooms_from_room_plans(self, room_plans: Any) -> dict[str, str]:
        """Try to extract room names from PROP_ROOM_PLANS property."""
        rooms: dict[str, str] = {}
        if not room_plans:
            return rooms
        plans = room_plans if isinstance(room_plans, list) else []
        if isinstance(room_plans, dict):
            plans = room_plans.get("data", room_plans.get("rooms", []))
        for room in plans:
            if not isinstance(room, dict):
                continue
            room_id = str(room.get("id", room.get("roomId", "")))
            name = room.get("name", room.get("roomName", room_id))
            if room_id.startswith("ROOM_"):
                rooms[room_id] = name
        return rooms

    def _persist_rooms(self) -> None:
        """Persist discovered rooms through HA's config-entry API."""
        if self.entry.options.get("cached_rooms") != self._rooms:
            self.hass.config_entries.async_update_entry(
                self.entry,
                options={**self.entry.options, "cached_rooms": dict(self._rooms)},
            )

    async def async_get_map_policy(self) -> tuple[dict[str, Any], str]:
        """Get fresh read credentials, as the S10 app's getS3Policy does.

        Shadow property 1130 contains an old credential snapshot. The app uses
        task/getPolicy to obtain a current session without starting a task.
        """
        await self._ensure_token()
        session = async_get_clientsession(self.hass)
        async with session.post(
            f"{self.wonderlab_endpoint}/sweeper/v1/task/getPolicy",
            headers=self._headers(),
            json={"deviceID": self.device_mac},
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise UpdateFailed(f"Map policy request failed (HTTP {response.status})")
            result = await response.json()

        if not isinstance(result, dict) or result.get("resultCode") != 100:
            code = _status_code_for_log(
                result.get("resultCode") if isinstance(result, dict) else None
            )
            raise UpdateFailed(f"Map policy request failed (resultCode {code})")
        policy = result.get("data")
        fields = ("AccessKeyId", "SecretAccessKey", "SessionToken")
        if not isinstance(policy, dict) or not all(
            isinstance(policy.get(key), str) and policy[key] for key in fields
        ):
            raise UpdateFailed("Map policy credentials are incomplete")

        expiration = policy.get("Expiration")
        try:
            if isinstance(expiration, str):
                parsed = datetime.fromisoformat(expiration)
                if parsed.tzinfo is None:
                    raise ValueError("Missing timezone")
                expires_at = parsed.timestamp()
            elif type(expiration) in (int, float):
                expires_at = float(expiration)
            else:
                raise ValueError("Invalid expiration")
        except (ValueError, OverflowError):
            raise UpdateFailed("Map policy expiration is invalid") from None
        if not math.isfinite(expires_at) or not expires_at > time.time() + 30:
            raise UpdateFailed("Map policy credentials are already expired")

        region = policy.get("awsRegion") or S3_REGION
        if not isinstance(region, str):
            raise UpdateFailed("Map policy region is invalid")
        return {
            "accessKeyId": policy["AccessKeyId"],
            "secretAccessKey": policy["SecretAccessKey"],
            "sessionToken": policy["SessionToken"],
        }, region

    async def _async_map_request(self, path: str, extra: dict | None = None) -> dict:
        """Read the app's map feed (monitor open/close never starts motion)."""
        async with async_get_clientsession(self.hass).post(
            f"{self.wonderlab_endpoint}/sweeper/v1/{path}", headers=self._headers(),
            json={"deviceID": self.device_mac, **(extra or {})},
            timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise UpdateFailed(f"Map feed request failed (HTTP {response.status})")
            result = await response.json()
        if not isinstance(result, dict) or result.get('resultCode') != 200:
            raise UpdateFailed('Map feed request failed')
        return result.get('data') or {}

    async def async_stop_maps(self) -> None:
        """Cancel route refresh and release monitoring on unload or HA stop."""
        self._map_stopping = True
        if self._route_task is not None and not self._route_task.done():
            self._route_task.cancel()
            try:
                await self._route_task
            except asyncio.CancelledError:
                pass
        await self.async_close_map_monitor()

    async def async_close_map_monitor(self) -> None:
        """Release the app-style map monitor when idle or unloaded."""
        if self._map_monitor_open:
            try:
                await self._async_map_request('task/monitor/close')
            except Exception as exc:
                _LOGGER.debug('Map monitor close failed (%s)', type(exc).__name__)
            finally:
                self._map_monitor_open = False

    def _clear_robot_position(self) -> None:
        if self.map_data is not None and self.map_data['attributes'].get('robot_position_available'):
            self.map_data = self.map_data | {
                'image': self.map_data.get('_history_image', self.map_data['_base_image']),
                'attributes': self.map_data['attributes'] | {
                    'robot_position_available': False, 'robot_position': None,
                },
            }
            self.map_updated = datetime.now().astimezone()

    async def async_refresh_position(self, props: dict) -> None:
        """Poll only during activity; expire poses when the feed stops advancing."""
        if self.map_data is None or self._map_stopping:
            return
        active = props.get(PROP_WORK_STATUS) in (5, 7, 8, 9, 15, 19)
        task_info = props.get(1032)
        task_id = task_info.get('taskId') if isinstance(task_info, dict) else None
        map_info = props.get(PROP_MAP_INFO)
        different_map = isinstance(map_info, dict) and map_info.get('mapID') and map_info['mapID'] != self.map_data['attributes']['map_id']
        if not active or not task_id or different_map:
            await self.async_close_map_monitor()
            self._clear_robot_position()
            self._position_key = None
            return
        try:
            if not self._map_monitor_open:
                await self._async_map_request('task/monitor/open')
                self._map_monitor_open = True
            if task_id != self._position_task:
                self._position_task, self._position_index = task_id, 0
            data = await self._async_map_request('task/trackPose', {
                'taskID': task_id, 'startIndex': self._position_index,
                'endIndex': self._position_index + 10000,
            })
            if type(data.get('endIndex')) is int and data['endIndex'] >= self._position_index:
                self._position_index = data['endIndex']
            points = data.get('data')
            if isinstance(points, list) and points:
                pose = points[-1]
                if not isinstance(pose, list) or len(pose) < 4 or not all(
                    type(v) in (int, float) and math.isfinite(v) for v in pose[:4]
                ):
                    raise ValueError('Invalid robot pose')
                key = (task_id, pose[3])
                if key != self._position_key:
                    snapshot = self.map_data
                    rendered = await self.hass.async_add_executor_job(render_position, snapshot, pose)
                    if self.map_data is snapshot:
                        self._position_key = key
                        self._position_received_at = time.time()
                        self.map_data = snapshot | {
                            'image': rendered['image'],
                            'attributes': snapshot['attributes'] | {
                                'robot_position_available': True,
                                'robot_position': {
                                    'kind': 'last_received', 'task_id': task_id,
                                    'received_at': datetime.now().astimezone().isoformat(),
                                    'x': pose[0], 'y': pose[1], 'heading': pose[2],
                                    'time_offset_ms': pose[3], 'pixel': rendered['pixel'],
                                },
                            },
                        }
                        self.map_updated = datetime.now().astimezone()
        except Exception as exc:
            _LOGGER.debug('Position feed unavailable (%s)', type(exc).__name__)
        if time.time() - self._position_received_at >= 60:
            self._clear_robot_position()

    async def async_get_latest_clean_record(self) -> dict[str, Any] | None:
        """Read the app's completed cleaning reports; never start a task."""
        await self._ensure_token()
        async with async_get_clientsession(self.hass).post(
            f"{self.wonderlab_endpoint}/sweeper/v1/taskRecord/list",
            headers=self._headers(), json={"deviceID": self.device_mac, "page": 0},
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT), allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise UpdateFailed(f"Cleaning report request failed (HTTP {response.status})")
            result = await response.json()
        if not isinstance(result, dict) or result.get("resultCode") != 200:
            raise UpdateFailed("Cleaning report request failed")
        data = result.get("data")
        records = data.get("recordList", []) if isinstance(data, dict) else []
        records = [r for r in records if isinstance(r, dict)
                   and r.get("action") in ("clean_rooms", "clean_all", "clean_area", "clean_zone")
                   and r.get("status") == 1 and isinstance(r.get("taskID"), str)
                   and type(r.get("timestamp")) in (int, float)
                   and isinstance(r.get("data"), dict)
                   and isinstance(r["data"].get("resource"), str)]
        return max(records, key=lambda r: r["timestamp"]) if records else None

    async def async_refresh_last_route(self, force: bool = False) -> None:
        """Fetch a new completed route, preserving the map if it fails."""
        if self.map_data is None or self._map_stopping or self._route_refresh_lock.locked():
            return
        async with self._route_refresh_lock:
            self._last_route_attempt = time.time()
            snapshot = self.map_data
            try:
                record = await self.async_get_latest_clean_record()
                if not record:
                    return
                current = snapshot['attributes'].get('last_route', {})
                if not force and current.get('task_id') == record['taskID']:
                    return
                creds, region = await self.async_get_map_policy()
                route = await self.hass.async_add_executor_job(
                    download_route, creds, self._map_bucket,
                    record['data']['resource'], region, snapshot, record,
                )
                if self.map_data is not snapshot:
                    return  # A simultaneous map refresh superseded these pixels.
                if route.get('status') == 'available':
                    self.map_data = snapshot | {
                        'image': route['image'], '_history_image': route['image'],
                        '_history_rows': route['_history_rows'],
                        'attributes': snapshot['attributes'] | {
                            'route_available': True, 'route_status': 'available',
                            'robot_position_available': False, 'robot_position': None,
                            'last_route': route['metadata'],
                        },
                    }
                    self._position_key = None
                    self.map_updated = datetime.now().astimezone()
                    if self.data:
                        self.async_set_updated_data(self.data | {'rooms': self._rooms})
                else:
                    # Never display a route belonging to another floor/map.
                    self.map_data = snapshot | {'attributes': snapshot['attributes'] | {
                        'route_status': route.get('status', 'unavailable'),
                    }}
            except Exception as exc:
                _LOGGER.warning('Failed to refresh historical route (%s)', type(exc).__name__)

    async def async_refresh_rooms(self) -> None:
        """Refresh rooms — branches per device type."""
        if self._is_k10() or self._is_k10_pro():
            await self.async_refresh_k10_rooms()
            return

        # S10: download map from S3
        try:
            props = await self.async_get_properties(
                [PROP_MAP_INFO, PROP_S3_BUCKET, PROP_ROOM_PLANS]
            )
        except UpdateFailed:
            _LOGGER.warning("Failed to fetch map properties for room refresh")
            return

        room_plans = props.get(PROP_ROOM_PLANS)
        rooms_from_plans = self._extract_rooms_from_room_plans(room_plans)
        if rooms_from_plans and not isinstance(props.get(PROP_MAP_INFO), dict):
            self._rooms = rooms_from_plans
            self._last_room_refresh = time.time()
            self._persist_rooms()
            _LOGGER.info("Loaded %d rooms from room plans property", len(rooms_from_plans))
            return

        map_info = props.get(PROP_MAP_INFO)
        bucket = props.get(PROP_S3_BUCKET, "prod-eu-sweeper-origin")

        resource = None
        if isinstance(map_info, dict):
            resource = map_info.get("resource")

        if not resource:
            _LOGGER.warning("No map resource path found")
            return

        try:
            creds, region = await self.async_get_map_policy()
            map_data = await self.hass.async_add_executor_job(
                _download_map_rooms, creds, bucket, resource, region
            )
        except Exception as exc:
            # SDK exceptions may contain signed URLs or cloud metadata.
            _LOGGER.warning(
                "Failed to download or parse S3 map (%s)", type(exc).__name__
            )
            return

        if map_data is not None:
            self._map_bucket = bucket
            self.map_data = map_data
            self.map_updated = datetime.now().astimezone()
            rooms = map_data["rooms"]
            self._rooms = rooms
            self._last_room_refresh = time.time()
            self._persist_rooms()
            _LOGGER.info("Loaded %d rooms from map", len(rooms))
            await self.async_refresh_last_route(force=True)

    async def _background_room_refresh(self) -> None:
        """Refresh rooms in the background so it doesn't block coordinator updates."""
        # Keep failed background downloads from requesting a policy every poll.
        now = time.monotonic()
        if self._last_room_attempt is not None and now - self._last_room_attempt < 300:
            return
        self._last_room_attempt = now
        try:
            await self.async_refresh_rooms()
            if self._rooms:
                self.async_set_updated_data(self.data | {"rooms": self._rooms})
        except Exception as exc:
            _LOGGER.debug("Background room refresh failed (%s)", type(exc).__name__)

    @property
    def rooms(self) -> dict[str, str]:
        """Return room ID to name mapping."""
        return self._rooms

    async def _ensure_token(self) -> None:
        """Refresh token if needed."""
        if not self.access_token or time.time() >= self._token_expiry:
            await self.async_login()

    async def _async_update_k10_track(self, working_status: Any) -> None:
        """Load the whole last track once per map, then only new points while active."""
        api = getattr(self, "_k10_api", None)
        if api is None or getattr(self, "map_data", None) is None:
            return
        active = working_status in K10_TRACK_ACTIVE_STATUSES
        if self._k10_track_loaded and not active:
            return
        start = self._k10_track_index if self._k10_track_loaded else 0
        try:
            session = async_get_clientsession(self.hass)
            points, total = await api.get_clean_track(session, start)
            if start and total < start:  # A new task restarted the track.
                start = 0
                points, total = await api.get_clean_track(session, 0)
            self.k10_track_error = None
        except K10ProtocolError as exc:
            self.k10_track_error = str(exc)
            self._k10_track_loaded = True  # Retry after the next map refresh, not every poll.
            _LOGGER.debug("K10+ cleaning track unavailable: %s", exc)
            return
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            self.k10_track_error = type(exc).__name__
            _LOGGER.debug("K10+ cleaning track unavailable (%s)", type(exc).__name__)
            return
        new_points = parse_path(points)
        self._k10_path = (self._k10_path if start else []) + new_points
        self._k10_track_index = start + len(points)
        self._k10_track_loaded = True

    async def _async_update_k10_overlay(self, raw_pose: Any) -> None:
        """Redraw the path and robot when either changes."""
        pose = parse_pose(raw_pose)
        snapshot = getattr(self, "map_data", None)
        path = self._k10_path
        if snapshot is None or "_frame" not in snapshot:
            return
        if pose == self._k10_pose and len(path) == self._k10_path_drawn:
            return
        try:
            rendered = await self.hass.async_add_executor_job(
                render_k10_overlay, snapshot, pose, path
            )
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            _LOGGER.debug("K10+ overlay not drawn (%s)", type(exc).__name__)
            return
        if self.map_data is not snapshot:
            return  # A map refresh replaced these pixels meanwhile.
        self._k10_pose, self._k10_path_drawn = pose, len(path)
        now = datetime.now().astimezone()
        attributes = snapshot["attributes"] | {
            "route_available": bool(rendered["path_segments"]),
            "route_points": len(path),
        }
        if rendered["pixel"] is not None:
            attributes |= {
                "robot_position_available": True,
                "robot_position": {
                    "kind": "reported", "received_at": now.isoformat(),
                    "x": pose[0], "y": pose[1], "heading": pose[2], "pixel": rendered["pixel"],
                },
                "vacuum_position": {"x": pose[0], "y": pose[1], "a": pose[2]},
            }
        self.map_data = snapshot | {"image": rendered["image"], "attributes": attributes}
        self.map_updated = now

    async def _async_update_data_k10(self) -> dict[str, Any]:
        """Fetch real-time status data from K10+ via getstatus."""
        status = await self.async_get_k10_status()

        if time.time() - self._last_room_refresh > 86400:
            self.hass.async_create_task(self._background_room_refresh())

        properties = {}
        if botslab_region(self.wonderlab_endpoint):
            try:
                session = async_get_clientsession(self.hass)
                api = await self._authorize_k10_api(session)
                properties = await api.get_properties(session)
            except (
                aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, K10ProtocolError
            ) as exc:
                _LOGGER.debug("K10+ control properties unavailable (%s)", type(exc).__name__)
        if properties:
            self.k10_property_shapes = {
                str(k): type(v).__name__ for k, v in sorted(properties.items())
            }
            # The robot uploads a new map package after cleaning/mapping.
            marker = properties.get("MapPkgOssInfo")
            if marker and self._k10_map_marker not in (None, marker):
                self._last_room_refresh = 0
            self._k10_map_marker = marker or self._k10_map_marker
            working_status = properties.get("WorkingStatus", status.get("WorkingStatus"))
            # Follow the path more closely while the robot is moving.
            interval = timedelta(seconds=(
                K10_ACTIVE_UPDATE_SECONDS if working_status in K10_MOVING_STATUSES
                else UPDATE_INTERVAL_SECONDS
            ))
            if self.update_interval != interval:
                self.update_interval = interval
            await self._async_update_k10_track(working_status)
            await self._async_update_k10_overlay(properties.get("CurrentLocation"))
        mode = next((name for name, value in CLEAN_MODES.items()
                     if value == properties.get("MopModeSwitching")), None)

        return {
            "online": status.get("online_status") == "online",
            "battery": status.get("BatteryLevel", 0),
            "work_status": status.get("WorkingStatus", K10_WORK_STATUS_STANDBY),
            "error_code": 0,
            "clean_mode": {
                "fan_level": properties.get("SuctionPowLevel", status.get("SuctionPowLevel", 1)),
                "type": mode,
                "times": 1,
                "water_level": 1,
            },
            "k10_controls_available": bool(properties),
            "mopping_pad_installed": properties.get("MoppingPadInstalled"),
            "clean_summary": {},
            "firmware": "",
            "rooms": self._rooms,
        }

    async def _async_update_data_k10_pro(self) -> dict[str, Any]:
        """Fetch real-time status data from K10+ Pro via getstatus."""

        if time.time() - self._last_room_refresh > 86400:
            self.hass.async_create_task(self._background_room_refresh())

        props = await self.async_get_properties(K10PRO_STATUS_PROPS)

        return {
            "online": props.get(K10PRO_PROP_ONLINE, 1) == 1,
            "battery": props.get(K10PRO_PROP_BATTERY, 0),
            "work_status": props.get(K10PRO_PROP_WORK_STATUS, 0),
            "error_code": 0,
            "clean_mode": {
                "fan_level": props.get(K10PRO_PROP_SUCTION_POW_LEVEL, 1),
                "type": "sweep",
                "times": 1,
                "water_level": 1,
            },
            "clean_summary": {},
            "firmware": "",
            "rooms": self._rooms,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch status data from device."""
        await self._ensure_token()

        if not self.device_mac:
            self.device_mac = self.entry.data.get(CONF_DEVICE_MAC)

        if self._is_k10():
            return await self._async_update_data_k10()

        if self._is_k10_pro():
            return await self._async_update_data_k10_pro()

        props = await self.async_get_properties(STATUS_PROPS + [1032, PROP_MAP_INFO])
        await self.async_refresh_position(props)

        if time.time() - self._last_room_refresh > 86400:
            self.hass.async_create_task(self._background_room_refresh())

        if not self._map_stopping and self.map_data is not None and time.time() - self._last_route_attempt > 60:
            self._last_route_attempt = time.time()
            self._route_task = self.hass.async_create_task(self.async_refresh_last_route())

        return {
            "online": props.get(PROP_ONLINE, False),
            "battery": props.get(PROP_BATTERY, 0),
            "work_status": props.get(PROP_WORK_STATUS, 1),
            "error_code": props.get(PROP_ERROR_CODE, 0),
            "clean_mode": props.get(PROP_CLEAN_MODE, {}),
            "clean_summary": props.get(PROP_CLEAN_SUMMARY, {}),
            "firmware": props.get(PROP_FIRMWARE, ""),
            "rooms": self._rooms,
        }
