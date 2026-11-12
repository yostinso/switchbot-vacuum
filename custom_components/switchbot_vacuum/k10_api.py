"""K10+ room protocol reconstructed from SwitchBot Android 10.6.

The account token stays on SwitchBot; the delegated token stays on Botslab.
Only the temporary OSS credentials returned for this upload go to storage.

The K10+ is a Qihoo 360 platform robot. SwitchBot's regional "wonderlabs" host
determines which regional 360 ("botslab") cloud holds the device.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import email.utils
import hashlib
import hmac
import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

if TYPE_CHECKING:
    from aiohttp import ClientSession

# Kept for backwards compatibility with callers/tests that use the EU constants.
API = "https://eu1-sapp-api.botslab.com"
WONDER = "https://wonderlabs.eu.api.switchbot.net"
APP_KEY = "toB_woan_adr"
# Shared signing constant embedded in the official Android SDK, not an account token.
APP_SECRET = "cec7fba0b848ae747f0af304ce4d1e54"
# Android 10.6 SweeperPlanModel.setWindModeType and SweepMopType enum.
ROOM_SUCTION = {1: "quiet", 2: "auto", 3: "strong", 4: "max"}
CLEAN_MODES = {"sweep": 0, "mop": 2}

# SwitchBot wonderlabs region label -> Qihoo 360 cloud region. The 360 regions
# (eu1, na1, ap1, eu2) are confirmed by independent Botslab client projects.
BOTSLAB_REGIONS = {"eu": "eu1", "us": "na1", "na": "na1", "ap": "ap1"}
_WONDER_RE = re.compile(r"^https://wonderlabs\.([a-z0-9]+)\.api\.switchbot\.net/?$")
_OSS_ENDPOINT_RE = re.compile(r"^oss-[a-z0-9-]+\.aliyuncs\.com$")
_STORAGE_SUFFIXES = (".aliyuncs.com", ".amazonaws.com")


class K10ProtocolError(Exception):
    """A validated K10+ operation could not be completed."""


def botslab_region(wonder_endpoint: str | None) -> str | None:
    """Return the 360 cloud region for a SwitchBot regional endpoint, if known."""
    match = _WONDER_RE.match(wonder_endpoint or "")
    return BOTSLAB_REGIONS.get(match.group(1)) if match else None


def api_host(region: str) -> str:
    return f"https://{region}-sapp-api.botslab.com"


def trusted_storage_url(url: str, region: str) -> bool:
    """Signed map URLs must point at this region's 360 storage buckets."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https"
        and host.endswith(_STORAGE_SUFFIXES)
        and host.split(".", 1)[0].startswith(f"{region}-")
    )


def parse_package(raw: bytes | str) -> list[dict[str, Any]]:
    """Decode a map package: one JSON object or several concatenated records.

    360 robots upload either a single map object or a stream of JSON records
    (some wrapped as ``{"infoType": ..., "data": {...}}``). Return the dicts.
    """
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    decoder = json.JSONDecoder()
    records: list[dict[str, Any]] = []
    index, length = 0, len(text)
    while index < length:
        while index < length and text[index] in " \r\n\t\x00":
            index += 1
        if index >= length:
            break
        obj, index = decoder.raw_decode(text, index)
        if isinstance(obj, dict):
            data = obj.get("data")
            records.append(data if isinstance(data, dict) and "infoType" in obj else obj)
    if not records:
        raise ValueError("Empty map package")
    return records


def select_map_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge the room record and the occupancy-grid record into one map dict."""
    merged: dict[str, Any] = {}
    for record in records:
        if "smartArea" in record or ("map" in record and "width" in record):
            for key, value in record.items():
                merged.setdefault(key, value)
    if "smartArea" not in merged and not ("map" in merged and "width" in merged):
        raise ValueError("Map package has neither rooms nor an occupancy grid")
    return merged


def signed_params(
    m2: str, *, timestamp: int | None = None, nonce: str | None = None
) -> dict[str, str]:
    sign_ts = str(int(time.time()) if timestamp is None else timestamp)
    sign_no = uuid.uuid4().hex if nonce is None else nonce
    signature = hashlib.md5((APP_KEY + m2 + sign_ts + sign_no + APP_SECRET).encode()).hexdigest()
    return {
        "appkey": APP_KEY,
        "m2": m2,
        "sign_ts": sign_ts,
        "sign_no": sign_no,
        "sign": signature,
        "appver": "10.6",
        "ci_osver": "16",
        "ci_brand": "google",
        "ci_model": "Pixel 7 Pro",
        "ci_lang": "en",
        "ci_tz": "UTC",
    }


def smart_area(map_data: dict[str, Any]) -> dict[str, Any]:
    area = map_data["smartArea"]
    return json.loads(area) if isinstance(area, str) else area


def room_names(map_data: dict[str, Any]) -> dict[str, str]:
    return {
        str(room["id"]): base64.b64decode(room["name"], validate=True).decode("utf-8")
        for room in smart_area(map_data)["value"]
    }


def prepare_rooms(
    map_data: dict[str, Any], requested: list[str], times: int, force_order: bool,
    fan_level: int | None = None,
) -> dict[str, Any]:
    """Validate every requested room before changing the robot or uploading."""
    if not requested or times not in (1, 2):
        raise K10ProtocolError("Select at least one room and one or two passes")
    if fan_level is not None and fan_level not in ROOM_SUCTION:
        raise K10ProtocolError("K10+ fan_level must be 1 (quiet) through 4 (max)")
    names = room_names(map_data)
    selected = []
    for requested_room in requested:
        text = str(requested_room)
        matches = [
            rid
            for rid, name in names.items()
            if text == rid or text == f"room{rid}" or text.casefold() == name.casefold()
        ]
        if len(matches) != 1:
            raise K10ProtocolError(f"Room is unknown or ambiguous: {text}")
        room_id = int(matches[0])
        if room_id not in selected:
            selected.append(room_id)
    payload = copy.deepcopy(smart_area(map_data))
    if str(payload["mapId"]) != str(map_data["mapId"]):
        raise K10ProtocolError("Room data does not belong to the current map")
    payload.update(
        activeIds=selected, areaCleanActiveId=[], cleanTimes=times, autoOrder=not force_order
    )
    if fan_level is not None:
        payload["isAttrOn"] = 1
    for room in payload["value"]:
        if room["id"] in selected:
            room["cleanTimes"] = times
            if fan_level is not None:
                room["windMode"] = ROOM_SUCTION[fan_level]
    return payload


def oss_request(
    info: dict[str, Any], content: bytes, *, date: str | None = None, region: str = "eu1"
) -> tuple[str, dict[str, str], str]:
    """Build the documented OSS V1 PUT; never forward account tokens to OSS."""
    if info.get("provider") != "aliyun":
        # Report the provider name only; it is needed to add support and is not secret.
        raise K10ProtocolError(
            f"Room upload storage provider {str(info.get('provider'))[:20]!r} is not supported"
        )
    if not re.fullmatch(rf"{re.escape(region)}-ali-\d+days", str(info.get("bucket", ""))) or (
        not _OSS_ENDPOINT_RE.fullmatch(str(info.get("endpoint", "")))
    ):
        raise K10ProtocolError("Unsupported room upload storage destination")
    key = info["path"]
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise K10ProtocolError("Invalid room upload path")
    sts = info["sts"]
    date = email.utils.formatdate(usegmt=True) if date is None else date
    md5 = base64.b64encode(hashlib.md5(content).digest()).decode("ascii")
    content_type = "text/plain"
    canonical = (
        f"PUT\n{md5}\n{content_type}\n{date}\n"
        f"x-oss-security-token:{sts['security_token']}\n/{info['bucket']}/{key}"
    )
    signature = base64.b64encode(
        hmac.new(sts["accesskey_secret"].encode(), canonical.encode(), hashlib.sha1).digest()
    ).decode("ascii")
    host = f"{info['bucket']}.{info['endpoint']}"
    return (
        f"https://{host}/{quote(key, safe='/')}",
        {
            "Content-Type": content_type,
            "Content-MD5": md5,
            "Date": date,
            "x-oss-security-token": sts["security_token"],
            "Authorization": f"OSS {sts['accesskey_id']}:{signature}",
        },
        f"aliyun://{host}/{key}",
    )


class K10Api:
    """Keep delegated credentials in memory; serialize room starts."""

    def __init__(self, device_mac: str, product_key: str, installation_id: str) -> None:
        self.device_mac = device_mac
        self.product_key = product_key
        self.installation_id = installation_id
        self.token: str | None = None
        self.token_expiry: float = 0
        self.region = "eu1"
        self.map_source: str | None = None
        self.map_record_count = 0
        self.lock = asyncio.Lock()

    @property
    def api(self) -> str:
        return api_host(self.region)

    async def authorize(
        self, session: ClientSession, wonder_endpoint: str, access_token: str, group_id: str
    ) -> None:
        region = botslab_region(wonder_endpoint)
        if region is None:
            raise K10ProtocolError("K10+ room support is not available for this SwitchBot region")
        if region != self.region:
            self.region, self.token, self.token_expiry = region, None, 0
        if self.token and self.token_expiry > time.time() + 60:
            return
        async with (
            asyncio.timeout(30),
            session.post(
                wonder_endpoint.rstrip("/") + "/wonder/sweeper360/v1/auth/applyToken",
                headers={"authorization": access_token},
                json={"groupID": group_id},
                allow_redirects=False,
            ) as response,
        ):
            response.raise_for_status()
            result = await response.json()
        body = result.get("body") or {}
        if result.get("statusCode") != 100 or not body.get("token"):
            raise K10ProtocolError("Could not obtain a delegated vacuum token")
        self.token = body["token"]
        self.token_expiry = float(body["valid_time"])

    async def request(
        self, session: ClientSession, path: str, *, form: dict[str, str] | None = None, **extra: Any
    ) -> dict[str, Any]:
        if not self.token:
            raise K10ProtocolError("Vacuum session has not been authorized")
        params = signed_params(self.installation_id)
        params.update(product_key=self.product_key, device_name=self.device_mac)
        params.update(extra)
        method = "POST" if form is not None else "GET"
        async with (
            asyncio.timeout(30),
            session.request(
                method,
                self.api + path,
                params=params,
                data=form,
                headers={"Authorization": "tokens " + self.token},
                allow_redirects=False,
            ) as response,
        ):
            if response.status >= 300:
                raise K10ProtocolError(f"Vacuum API returned HTTP {response.status}")
            return await response.json()

    async def get_properties(self, session: ClientSession) -> dict[str, Any]:
        """Read current mode, pad presence and device properties."""
        result = await self.request(session, "/v1/iot/device/get_property")
        if result.get("code") != 0:
            raise K10ProtocolError(f"Cannot read device properties (code {result.get('code')})")
        properties = result["data"]["result"]
        properties = json.loads(properties) if isinstance(properties, str) else properties
        if not isinstance(properties, dict):
            raise K10ProtocolError("Invalid K10+ property response")
        return properties

    async def set_properties(self, session: ClientSession, items: dict[str, int]) -> None:
        """Set only caller-validated properties using the app's form encoding."""
        result = await self.request(
            session, "/v1/iot/device/set_property",
            form={"product_key": self.product_key, "device_name": self.device_mac,
                  "items": json.dumps(items, separators=(",", ":"))},
        )
        if result.get("code") != 0:
            raise K10ProtocolError(f"Device setting rejected (code {result.get('code')})")

    @staticmethod
    def validate_mode(mode: str, properties: dict[str, Any]) -> None:
        """Mirror the app's requirement to install/remove the mop plate."""
        if mode not in CLEAN_MODES:
            raise K10ProtocolError("K10+ supports sweep or mop, not simultaneous sweep_mop")
        installed = properties.get("MoppingPadInstalled")
        if mode == "mop" and installed is not True:
            raise K10ProtocolError("Install the mopping pad plate before selecting mop mode")
        if mode == "sweep" and installed is not False:
            raise K10ProtocolError("Remove the mopping pad plate before selecting vacuum mode")

    async def _set_mode(
        self, session: ClientSession, mode: str, properties: dict[str, Any]
    ) -> None:
        self.validate_mode(mode, properties)
        if properties.get("MopModeSwitching") == CLEAN_MODES[mode]:
            return
        if properties.get("WorkingStatus") not in (0, 4, 6, 7):
            raise K10ProtocolError("Pause the current task before changing cleaning mode")
        await self.set_properties(session, {"MopModeSwitching": CLEAN_MODES[mode]})
        # Do not send a start while a mode change is still unconfirmed.
        for attempt in range(3):
            current = await self.get_properties(session)
            if current.get("MopModeSwitching") == CLEAN_MODES[mode]:
                self.validate_mode(mode, current)
                return
            if attempt < 2:
                await asyncio.sleep(1)
        raise K10ProtocolError("Cleaning mode was not confirmed; no cleaning start sent")

    async def set_mode(self, session: ClientSession, mode: str) -> None:
        async with self.lock:
            properties = await self.get_properties(session)
            await self._set_mode(session, mode, properties)

    async def set_suction(self, session: ClientSession, level: int) -> None:
        if level not in (0, 1, 2, 3):
            raise K10ProtocolError("K10+ suction must be between 0 and 3")
        async with self.lock:
            await self.set_properties(session, {"SuctionPowLevel": level})

    async def empty_dustbin(self, session: ClientSession) -> None:
        async with self.lock:
            properties = await self.get_properties(session)
            if properties.get("WorkingStatus") not in (6, 7):
                raise K10ProtocolError("Dock the K10+ before emptying its dustbin")
            await self.invoke_service(session, "StartCollectingDust", {})

    async def _download_package(self, session: ClientSession, url: str) -> dict[str, Any] | None:
        """Fetch a signed map object; None if it has expired or holds no map."""
        if not trusted_storage_url(url, self.region):
            raise K10ProtocolError("Unexpected signed map destination")
        async with asyncio.timeout(30), session.get(url, allow_redirects=False) as response:
            if response.status in (403, 404):
                return None  # 1-day objects expire; try the recorded map instead.
            if response.status != 200:
                raise K10ProtocolError(f"Map storage returned HTTP {response.status}")
            raw = await response.read()
        if len(raw) > 32 * 1024 * 1024:
            raise K10ProtocolError("Map package is too large")
        try:
            records = parse_package(raw)
            map_data = select_map_record(records)
        except ValueError:
            return None
        self.map_record_count = len(records)
        return map_data

    async def get_map(
        self, session: ClientSession, *, strict: bool = True
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the current map and the device's MapPkgOssInfo.

        ``strict`` (used before cleaning) requires the map to belong to the
        device's current MapID; display refreshes also accept the recorded map.
        """
        result = await self.request(session, "/v1/iot/device/get_property", extend=1)
        if result.get("code") != 0:
            raise K10ProtocolError(f"Cannot read map properties (code {result.get('code')})")
        data = result["data"]
        properties = data["result"]
        properties = json.loads(properties) if isinstance(properties, str) else properties
        signs = (data.get("extend") or {}).get("s3_signs") or {}

        def as_dict(value: Any) -> dict[str, Any]:
            value = json.loads(value) if isinstance(value, str) and value else value
            return value if isinstance(value, dict) else {}

        map_info = as_dict(properties.get("MapPkgOssInfo"))
        map_id = str(map_info["MapID"]) if map_info.get("MapID") is not None else None
        candidates: list[tuple[str, str]] = []
        if map_info.get("Url") in signs:
            candidates.append(("current", signs[map_info["Url"]]))
        for recorded in as_dict(properties.get("RecordedMapList")).get("RecordedMaps") or []:
            if not isinstance(recorded, dict):
                continue
            if map_id and str(recorded.get("MapID", map_id)) != map_id:
                continue
            for key in ("PermSaveUrl", "PermBinSaveUrl"):
                if recorded.get(key) in signs:
                    candidates.append(("recorded", signs[recorded[key]]))
        if not candidates:
            raise K10ProtocolError("The vacuum has not uploaded a map yet")

        for source, url in candidates:
            map_data = await self._download_package(session, url)
            if map_data is None:
                continue
            if strict and map_id and str(map_data.get("mapId", map_id)) != map_id:
                continue
            if strict and source == "current" and map_data.get("cleanId") != map_info.get("CleanID"):
                raise K10ProtocolError("Map file and device metadata do not match")
            if strict and "smartArea" not in map_data:
                continue
            self.map_source = source
            return map_data, map_info
        raise K10ProtocolError("No usable map package was found")

    async def clean_rooms(
        self, session: ClientSession, requested: list[str], times: int = 1, force_order: bool = True,
        mode: str | None = None, fan_level: int | None = None,
    ) -> None:
        async with self.lock:
            map_data, map_info = await self.get_map(session)
            payload = prepare_rooms(map_data, requested, times, force_order, fan_level)
            properties = await self.get_properties(session)
            if mode is None:
                mode = next((name for name, value in CLEAN_MODES.items()
                             if value == properties.get("MopModeSwitching")), None)
                if mode is None:
                    raise K10ProtocolError("Cannot determine the current cleaning mode")
            self.validate_mode(mode, properties)
            await self._set_mode(session, mode, properties)
            config = await self.request(
                session,
                "/v1/user/get_upload_config",
                file_type="txt",
                scene_id="robot_config",
                scene_info='{"id":"sweep_areas"}',
            )
            if config.get("code") != 0:
                raise K10ProtocolError("Could not obtain room upload configuration")
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            url, headers, robot_url = oss_request(
                config["data"]["upload_info"], content, region=self.region
            )
            async with (
                asyncio.timeout(30),
                session.put(url, headers=headers, data=content, allow_redirects=False) as response,
            ):
                if response.status != 200:
                    raise K10ProtocolError(
                        f"Room upload failed (HTTP {response.status}); no start sent"
                    )
            service_input = {
                "MapID": int(map_info["MapID"]),
                "CleanID": map_info["CleanID"],
                "Url": robot_url,
            }
            await self.invoke_service(session, "StartCleanInAreas", service_input)

    async def invoke_service(
        self, session: ClientSession, identifier: str, service_input: dict[str, Any]
    ) -> None:
        """Send once and require the robot's reply, for room starts and emptying."""
        form = {
            "product_key": self.product_key,
            "device_name": self.device_mac,
            "identifier": identifier,
            "input": json.dumps(service_input, separators=(",", ":")),
        }
        # Never retry this POST: transport failure may still mean it started.
        result = await self.request(session, "/v1/iot/device/invoke_service_async", form=form)
        if result.get("code") != 0:
            raise K10ProtocolError(f"{identifier} rejected (code {result.get('code')})")
        request_id = result["data"]["request_id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            result = await self.request(
                session, "/v1/iot/device/invoke_service_reply", request_id=request_id
            )
            data = result.get("data") or {}
            if result.get("code") == 0:
                reply = data["result"]
                reply = json.loads(reply) if isinstance(reply, str) else reply
                if reply.get("ErrorNO") != 0:
                    raise K10ProtocolError(
                        f"Robot rejected {identifier} (ErrorNO {reply.get('ErrorNO')})"
                    )
                return
            if data.get("errno") != 1306050:
                raise K10ProtocolError(
                    f"Cannot confirm {identifier} (code {result.get('code')}); check robot state"
                )
        raise K10ProtocolError(
            f"{identifier} confirmation timed out; check robot state before retrying"
        )
