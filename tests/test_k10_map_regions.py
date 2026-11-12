"""K10+ regional hosts, map package parsing, map rendering and diagnostics.

Synthetic maps only: no account, network or robot is used.
"""

import base64
import json
import struct
import sys
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.switchbot_vacuum import coordinator as coordinator_module
from custom_components.switchbot_vacuum import diagnostics, image
from custom_components.switchbot_vacuum import k10_api as api
from custom_components.switchbot_vacuum import k10_map
from custom_components.switchbot_vacuum.const import (
    DEVICE_TYPE_K10,
    DEVICE_TYPE_K10PRO,
    DEVICE_TYPE_S10,
    DOMAIN,
)

US = "https://wonderlabs.us.api.switchbot.net"


def lz4_literals(data: bytes) -> bytes:
    """Valid LZ4 block made of a single literal run."""
    n = len(data)
    out = bytearray([min(n, 15) << 4])
    if n >= 15:
        rest = n - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    return bytes(out) + data


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# 40 x 30 cells at 5 cm, origin (-1.0 m, -0.5 m). Grid row 0 is the bottom.
W, H, RES, X0, Y0 = 40, 30, 0.05, -1.0, -0.5


def grid_bytes() -> bytes:
    cells = bytearray(W * H)
    for r in range(5, 25):
        for c in range(5, 35):
            edge = r in (5, 24) or c in (5, 34)
            cells[r * W + c] = 255 if edge else 127
    return bytes(cells)


def mm(col: float, row: float) -> list[float]:
    """World millimetres for a continuous (col, row-from-bottom) grid position."""
    return [round((X0 + col * RES) * 1000, 3), round((Y0 + row * RES) * 1000, 3)]


def k10_map_record(*, vertex_scale=1.0) -> dict:
    left = [mm(6, 6), mm(20, 6), mm(20, 24), mm(6, 24)]
    right = [mm(20, 6), mm(34, 6), mm(34, 24), mm(20, 24)]
    def scale(poly):
        return [[x * vertex_scale, y * vertex_scale] for x, y in poly]

    return {
        "mapId": 101,
        "cleanId": "example-clean",
        "width": W,
        "height": H,
        "resolution": RES,
        "x_min": X0,
        "y_min": Y0,
        "map": base64.b64encode(lz4_literals(grid_bytes())).decode(),
        "chargeHandlePos": [v * vertex_scale for v in mm(30, 8)],
        "smartArea": {
            "mapId": 101,
            "value": [
                {"id": 1, "name": b64("Kitchen"), "vertexs": scale(left)},
                {"id": 2, "name": b64("Den"), "vertexs": scale(right)},
            ],
        },
    }


def pixel(rendered, x, y):
    row = rendered["_render_rows"][int(y)]
    return tuple(row[int(x) * 4 : int(x) * 4 + 4])


class RegionTests(unittest.TestCase):
    def test_known_regions(self):
        self.assertEqual(api.botslab_region("https://wonderlabs.eu.api.switchbot.net"), "eu1")
        self.assertEqual(api.botslab_region(US + "/"), "na1")
        self.assertEqual(api.botslab_region("https://wonderlabs.ap.api.switchbot.net"), "ap1")

    def test_unknown_or_foreign_hosts(self):
        for host in (None, "", "https://attacker.example", "http://wonderlabs.us.api.switchbot.net",
                     "https://wonderlabs.us.api.switchbot.net.attacker.example",
                     "https://wonderlabs.zz.api.switchbot.net"):
            self.assertIsNone(api.botslab_region(host), host)

    def test_storage_url_must_match_region(self):
        self.assertTrue(api.trusted_storage_url(
            "https://na1-ali-1days.oss-us-west-1.aliyuncs.com/x?sig=1", "na1"))
        self.assertFalse(api.trusted_storage_url(
            "https://eu1-ali-1days.oss-eu-central-1.aliyuncs.com/x", "na1"))
        self.assertFalse(api.trusted_storage_url("https://na1-x.attacker.example/x", "na1"))
        self.assertFalse(api.trusted_storage_url(
            "http://na1-ali-1days.oss-us-west-1.aliyuncs.com/x", "na1"))

    def test_upload_accepts_regional_bucket(self):
        info = {
            "provider": "aliyun", "bucket": "na1-ali-1days",
            "endpoint": "oss-us-west-1.aliyuncs.com", "path": "a/b.txt",
            "sts": {"accesskey_id": "k", "accesskey_secret": "s", "security_token": "t"},
        }
        url, _, robot_url = api.oss_request(info, b"{}", region="na1")
        self.assertTrue(url.startswith("https://na1-ali-1days.oss-us-west-1.aliyuncs.com/"))
        self.assertEqual(robot_url, "aliyun://na1-ali-1days.oss-us-west-1.aliyuncs.com/a/b.txt")
        with self.assertRaises(api.K10ProtocolError):
            api.oss_request(info, b"{}", region="eu1")
        with self.assertRaisesRegex(api.K10ProtocolError, "'aws'"):
            api.oss_request(info | {"provider": "aws"}, b"{}", region="na1")
        with self.assertRaises(api.K10ProtocolError):
            api.oss_request(info | {"endpoint": "attacker.example"}, b"{}", region="na1")


class Response:
    def __init__(self, *, data=None, status=200, raw=b""):
        self.data, self.status, self.raw = data, status, raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.data

    async def read(self):
        return self.raw

    def raise_for_status(self):
        pass


class Session:
    def __init__(self, replies=(), downloads=None):
        self.replies = iter(replies)
        self.downloads = downloads or {}
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response(data=next(self.replies))

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response(data=next(self.replies))

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        status, raw = self.downloads.get(str(url), (404, b""))
        return Response(status=status, raw=raw)


class RegionalSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_us_account_uses_us_hosts(self):
        client = api.K10Api("example-device", "example-product", "installation")
        session = Session([
            {"statusCode": 100, "body": {"token": "delegated", "valid_time": 9999999999}},
            {"code": 0},
        ])
        await client.authorize(session, US, "account-token", "family")
        await client.request(session, "/v1/iot/device/get_property")
        self.assertEqual(session.calls[0][1], US + "/wonder/sweeper360/v1/auth/applyToken")
        self.assertEqual(session.calls[1][1],
                         "https://na1-sapp-api.botslab.com/v1/iot/device/get_property")

    async def test_region_change_discards_token(self):
        client = api.K10Api("example-device", "example-product", "installation")
        client.token, client.token_expiry = "eu-token", 9999999999
        session = Session([{"statusCode": 100, "body": {"token": "us", "valid_time": 9999999999}}])
        await client.authorize(session, US, "account-token", "family")
        self.assertEqual((client.region, client.token), ("na1", "us"))


def property_reply(props, signs):
    return {"code": 0, "data": {"result": json.dumps(props), "extend": {"s3_signs": signs}}}


class MapDownloadTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client = api.K10Api("example-device", "example-product", "installation")
        client.region = "na1"
        return client

    async def test_expired_current_map_falls_back_to_recorded_map(self):
        current = "https://na1-ali-1days.oss-us-west-1.aliyuncs.com/cur"
        recorded = "https://na1-ali-180days.oss-us-west-1.aliyuncs.com/rec"
        record = k10_map_record()
        client = self.client()
        client.request = AsyncMock(return_value=property_reply(
            {
                "MapPkgOssInfo": json.dumps({"Url": "aliyun://cur", "MapID": 101, "CleanID": "c"}),
                "RecordedMapList": json.dumps({"RecordedMaps": [
                    {"MapID": 101, "PermSaveUrl": "aliyun://rec"}]}),
            },
            {"aliyun://cur": current, "aliyun://rec": recorded},
        ))
        session = Session(downloads={current: (404, b""),
                                     recorded: (200, json.dumps(record).encode())})
        data, info = await client.get_map(session, strict=False)
        self.assertEqual(client.map_source, "recorded")
        self.assertEqual(data["width"], W)
        self.assertEqual(info["MapID"], 101)

    async def test_strict_rejects_other_map(self):
        url = "https://na1-ali-1days.oss-us-west-1.aliyuncs.com/cur"
        client = self.client()
        client.request = AsyncMock(return_value=property_reply(
            {"MapPkgOssInfo": {"Url": "u", "MapID": 999, "CleanID": "example-clean"}},
            {"u": url},
        ))
        session = Session(downloads={url: (200, json.dumps(k10_map_record()).encode())})
        with self.assertRaises(api.K10ProtocolError):
            await client.get_map(session)

    async def test_attempts_are_recorded_without_signatures(self):
        current = ("https://na1-ali-1days.oss-us-west-1.aliyuncs.com/cur"
                   "?Expires=1&OSSAccessKeyId=KEYID&Signature=ab%2Bcd%3D")
        recorded = "https://na1-ali-180days.oss-us-west-1.aliyuncs.com/rec?Signature=SIG2"
        client = self.client()
        client.request = AsyncMock(return_value=property_reply(
            {
                "MapPkgOssInfo": json.dumps({"Url": "aliyun://cur", "MapID": 101}),
                "RecordedMapList": json.dumps({"RecordedMaps": [
                    {"MapID": 101, "PermBinSaveUrl": "aliyun://rec"}]}),
            },
            {"aliyun://cur": current, "aliyun://rec": recorded},
        ))
        package = zlib.compress(json.dumps(k10_map_record()).encode())
        session = Session(downloads={current: (404, b""), recorded: (200, package)})
        await client.get_map(session, strict=False)
        # The signed URL reaches aiohttp without being re-quoted.
        self.assertEqual(str(session.calls[0][1]), current)
        self.assertIn("%2B", str(session.calls[0][1]))
        attempts = client.map_attempts
        self.assertEqual([a["outcome"] for a in attempts], ["expired_or_denied", "ok"])
        self.assertEqual(attempts[1]["wrapper"], "zlib")
        self.assertEqual(attempts[1]["key"], "PermBinSaveUrl")
        self.assertEqual(client.map_inventory["recorded_maps"][0]["signed"], ["PermBinSaveUrl"])
        text = json.dumps([client.map_inventory, attempts])
        for secret in ("KEYID", "Signature", "SIG2", "aliyun://"):
            self.assertNotIn(secret, text)

    async def test_unparseable_package_is_described(self):
        url = "https://na1-ali-1days.oss-us-west-1.aliyuncs.com/cur"
        client = self.client()
        client.request = AsyncMock(return_value=property_reply(
            {"MapPkgOssInfo": {"Url": "u", "MapID": 1}}, {"u": url}))
        session = Session(downloads={url: (200, b"\x08\x96\x01binary")})
        with self.assertRaisesRegex(api.K10ProtocolError, "No usable map"):
            await client.get_map(session, strict=False)
        attempt = client.map_attempts[0]
        self.assertEqual(attempt["magic"], "08960162")
        self.assertTrue(attempt["outcome"].startswith("unparsed:"))

    async def test_display_refresh_may_use_another_recorded_map(self):
        url = "https://na1-ali-180days.oss-us-west-1.aliyuncs.com/rec"
        client = self.client()
        client.request = AsyncMock(return_value=property_reply(
            {"MapPkgOssInfo": {"MapID": 5},
             "RecordedMapList": {"RecordedMaps": [{"MapID": 101, "PermSaveUrl": "r"}]}},
            {"r": url},
        ))
        session = Session(downloads={url: (200, json.dumps(k10_map_record()).encode())})
        await client.get_map(session, strict=False)
        self.assertEqual(client.map_source, "recorded_other_map")
        with self.assertRaisesRegex(api.K10ProtocolError, "not uploaded"):
            await client.get_map(session, strict=True)

    async def test_no_uploaded_map(self):
        client = self.client()
        client.request = AsyncMock(return_value=property_reply({}, {}))
        with self.assertRaisesRegex(api.K10ProtocolError, "not uploaded"):
            await client.get_map(Session())


class PackageTests(unittest.TestCase):
    def test_single_object(self):
        self.assertEqual(api.parse_package(b'{"a": 1}'), [{"a": 1}])

    def test_concatenated_and_wrapped_records(self):
        raw = (json.dumps({"infoType": 20004, "data": {"map": "x", "width": 1}})
               + "\n" + json.dumps({"smartArea": {"value": []}, "mapId": 3}))
        merged = api.select_map_record(api.parse_package(raw.encode()))
        self.assertEqual(merged["width"], 1)
        self.assertEqual(merged["mapId"], 3)

    def test_unwrap_wrappers(self):
        import gzip
        import io
        import zipfile

        body = b'{"a": 1}'
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("readme.md", b"x")
            z.writestr("map.json", body)
        cases = {
            "gzip": gzip.compress(body), "zlib": zlib.compress(body),
            "zip": archive.getvalue(), "bom": b"\xef\xbb\xbf" + body, "none": body,
        }
        for wrapper, raw in cases.items():
            self.assertEqual(api.unwrap_package(raw), (body, wrapper), wrapper)

    def test_unwrap_is_bounded(self):
        bomb = zlib.compress(b"\0" * (api.MAX_PACKAGE_BYTES + 10))
        with self.assertRaises(ValueError):
            api.unwrap_package(bomb)

    def test_unrelated_package(self):
        with self.assertRaises(ValueError):
            api.select_map_record(api.parse_package(b'{"foo": 1}'))


class Lz4Tests(unittest.TestCase):
    def test_overlapping_match(self):
        self.assertEqual(k10_map.lz4_block_decompress(b"\x35abc\x03\x00", 12), b"abc" * 4)

    def test_long_literal(self):
        data = bytes(range(256)) * 3
        self.assertEqual(k10_map.lz4_block_decompress(lz4_literals(data), len(data)), data)

    def test_rejects_bad_offsets_and_overruns(self):
        for bad in (b"\x35abc\x09\x00", b"\x35abc\x00\x00", b"\x35abc\x03"):
            with self.assertRaises(ValueError):
                k10_map.lz4_block_decompress(bad, 12)
        with self.assertRaises(ValueError):
            k10_map.lz4_block_decompress(lz4_literals(b"x" * 20), 10)


class RenderTests(unittest.TestCase):
    def test_renders_valid_png_with_rooms_and_dock(self):
        rendered = k10_map.decode_k10_map(k10_map_record())
        png = rendered["image"]
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = struct.unpack(">2I", png[16:24])
        attrs = rendered["attributes"]
        self.assertEqual((width, height), (attrs["width"], attrs["height"]))
        self.assertEqual(rendered["rooms"], {"room1": "Kitchen", "room2": "Den"})
        self.assertEqual(attrs["coordinate_fit"],
                         {"hypothesis": "vertex_mm_origin_m", "points_on_grid": 1.0})
        self.assertIsNotNone(attrs["charger"])
        # Each room is painted in its own colour at its label point.
        kitchen, den = attrs["rooms"]["room1"], attrs["rooms"]["room2"]
        self.assertEqual(pixel(rendered, kitchen["x"], kitchen["y"])[:3], k10_map.ROOM_COLORS[0])
        self.assertEqual(pixel(rendered, den["x"], den["y"])[:3], k10_map.ROOM_COLORS[1])
        self.assertLess(kitchen["x"], den["x"])  # x is not mirrored
        # Unknown cells are transparent.
        self.assertEqual(pixel(rendered, 0, 0)[3], 0)
        # IDAT inflates to the expected size.
        idat_len = struct.unpack(">I", png[33:37])[0]
        raw = zlib.decompress(png[41 : 41 + idat_len])
        self.assertEqual(len(raw), height * (1 + width * 4))

    def test_y_axis_points_up_in_world(self):
        rendered = k10_map.decode_k10_map(k10_map_record())
        frame = k10_map._Frame(*rendered["_frame"])
        low = frame.pixel(*mm(10, 8))
        high = frame.pixel(*mm(10, 20))
        self.assertGreater(low[1], high[1])

    def test_calibration_points_round_trip(self):
        rendered = k10_map.decode_k10_map(k10_map_record())
        frame = k10_map._Frame(*rendered["_frame"])
        for point in rendered["attributes"]["calibration_points"]:
            px, py = frame.pixel(point["vacuum"]["x"], point["vacuum"]["y"])
            self.assertAlmostEqual(px, point["map"]["x"], places=0)
            self.assertAlmostEqual(py, point["map"]["y"], places=0)

    def test_vertices_in_metres_are_detected(self):
        rendered = k10_map.decode_k10_map(k10_map_record(vertex_scale=0.001))
        self.assertEqual(rendered["attributes"]["coordinate_fit"]["hypothesis"], "same_units")

    def test_rooms_without_grid_still_fail_cleanly(self):
        record = k10_map_record()
        record["map"] = "not base64!!"
        with self.assertRaises(ValueError):
            k10_map.decode_k10_map(record)

    def test_robot_position_overlay(self):
        rendered = k10_map.decode_k10_map(k10_map_record())
        x, y = mm(12, 12)
        result = k10_map.render_k10_position(rendered, (x, y, 90))
        self.assertNotEqual(result["image"], rendered["image"])
        frame = k10_map._Frame(*rendered["_frame"])
        self.assertAlmostEqual(result["pixel"]["x"], frame.pixel(x, y)[0], places=0)
        with self.assertRaises(ValueError):
            k10_map.render_k10_position(rendered, (999999, 999999, 0))

    def test_parse_pose(self):
        self.assertEqual(k10_map.parse_pose("[1, 2, 3]"), (1.0, 2.0, 3.0))
        self.assertEqual(k10_map.parse_pose([1, 2]), (1.0, 2.0, 0.0))
        self.assertEqual(k10_map.parse_pose({"x": 1, "y": 2, "angle": 5}), (1.0, 2.0, 5.0))
        for bad in (None, "x", [1], ["a", 2], [float("nan"), 1]):
            self.assertIsNone(k10_map.parse_pose(bad))

    def test_summary_has_structure_but_no_names_or_map(self):
        summary = k10_map.describe_map(k10_map_record(), 1)
        text = json.dumps(summary)
        self.assertNotIn("Kitchen", text)
        self.assertNotIn(b64("Kitchen"), text)
        self.assertEqual(summary["width"], W)
        self.assertTrue(summary["grid"]["decoded"])
        self.assertEqual(len(summary["rooms"]), 2)


def fake_coordinator(**extra):
    hass = MagicMock()

    async def run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=run)
    coordinator = object.__new__(coordinator_module.SwitchBotS10Coordinator)
    coordinator.hass = hass
    coordinator.entry = SimpleNamespace(data={"device_type": DEVICE_TYPE_K10}, options={})
    coordinator.device_mac = "FAKEDEVICE"
    coordinator.wonderlab_endpoint = US
    coordinator.map_data = None
    coordinator.map_updated = None
    coordinator._rooms = {}
    coordinator._last_room_refresh = 0
    coordinator._k10_pose = None
    coordinator._k10_map_marker = None
    coordinator._k10_api = None
    coordinator.k10_map_summary = None
    coordinator.k10_property_shapes = {}
    coordinator.k10_last_error = None
    coordinator.data = {}
    for key, value in extra.items():
        setattr(coordinator, key, value)
    return coordinator


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch.object(coordinator_module, "async_get_clientsession",
                               return_value=MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_room_refresh_renders_and_persists_map(self):
        coordinator = fake_coordinator()
        client = MagicMock(map_record_count=1, map_source="current")
        client.get_map = AsyncMock(return_value=(k10_map_record(), {"MapID": 101}))
        coordinator._authorize_k10_api = AsyncMock(return_value=client)
        coordinator._persist_rooms = MagicMock()
        await coordinator.async_refresh_k10_rooms()
        client.get_map.assert_awaited_once()
        self.assertFalse(client.get_map.await_args.kwargs["strict"])
        self.assertEqual(coordinator.rooms, {"room1": "Kitchen", "room2": "Den"})
        coordinator._persist_rooms.assert_called_once()
        self.assertEqual(coordinator.map_data["attributes"]["source"], "SwitchBot K10+ cloud map")
        self.assertEqual(coordinator.k10_map_summary["source"], "current")

    async def test_protocol_error_is_recorded(self):
        coordinator = fake_coordinator()
        coordinator._authorize_k10_api = AsyncMock(
            side_effect=api.K10ProtocolError("The vacuum has not uploaded a map yet"))
        await coordinator.async_refresh_k10_rooms()
        self.assertEqual(coordinator.k10_last_error, "The vacuum has not uploaded a map yet")
        self.assertIsNone(coordinator.map_data)

    async def test_position_updates_only_on_change(self):
        coordinator = fake_coordinator(map_data=k10_map.decode_k10_map(k10_map_record()))
        pose = mm(12, 12) + [45]
        await coordinator._async_update_k10_position(json.dumps(pose))
        attrs = coordinator.map_data["attributes"]
        self.assertTrue(attrs["robot_position_available"])
        self.assertEqual(attrs["vacuum_position"], {"x": pose[0], "y": pose[1], "a": 45})
        calls = coordinator.hass.async_add_executor_job.await_count
        await coordinator._async_update_k10_position(json.dumps(pose))
        self.assertEqual(coordinator.hass.async_add_executor_job.await_count, calls)

    async def test_status_poll_triggers_map_refresh_on_new_upload(self):
        coordinator = fake_coordinator(_last_room_refresh=10**12)
        coordinator.async_get_k10_status = AsyncMock(return_value={"WorkingStatus": 6})
        client = MagicMock()
        client.get_properties = AsyncMock(return_value={"MapPkgOssInfo": "a"})
        coordinator._authorize_k10_api = AsyncMock(return_value=client)
        await coordinator._async_update_data_k10()
        self.assertEqual(coordinator._last_room_refresh, 10**12)
        client.get_properties.return_value = {"MapPkgOssInfo": "b", "CurrentLocation": "[0,0,0]"}
        await coordinator._async_update_data_k10()
        self.assertEqual(coordinator._last_room_refresh, 0)
        self.assertEqual(coordinator.k10_property_shapes,
                         {"CurrentLocation": "str", "MapPkgOssInfo": "str"})

    async def test_diagnostics_are_redacted(self):
        coordinator = fake_coordinator(map_data=k10_map.decode_k10_map(k10_map_record()))
        coordinator._rooms = {"room1": "Kitchen"}
        coordinator._k10_api = SimpleNamespace(token="secret-token", map_source="current")
        coordinator.access_token = "account-secret"
        coordinator.k10_map_summary = k10_map.describe_map(k10_map_record(), 1)
        hass = MagicMock()
        entry = SimpleNamespace(entry_id="e", data={
            "device_type": DEVICE_TYPE_K10, "password": "pw-secret", "username": "user@x",
            "device_mac": "FAKEDEVICE"})
        hass.data = {DOMAIN: {"e": coordinator}}
        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        text = json.dumps(result)
        for secret in ("secret-token", "account-secret", "pw-secret", "user@x", "FAKEDEVICE",
                       "Kitchen"):
            self.assertNotIn(secret, text)
        self.assertEqual(result["region"]["botslab_region"], "na1")
        self.assertEqual(result["map"]["rooms_drawn"], 2)


class ImagePlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_map_entity_for_k10_and_s10_not_k10_pro(self):
        for device_type, expected in ((DEVICE_TYPE_K10, 1), (DEVICE_TYPE_S10, 1),
                                      (DEVICE_TYPE_K10PRO, 0)):
            add = MagicMock()
            coordinator = MagicMock(device_mac="FAKEDEVICE", map_updated=None)
            hass = MagicMock()
            hass.data = {DOMAIN: {"e": coordinator}}
            entry = SimpleNamespace(entry_id="e", data={"device_type": device_type})
            await image.async_setup_entry(hass, entry, add)
            self.assertEqual(sum(len(c.args[0]) for c in add.call_args_list), expected)


if __name__ == "__main__":
    unittest.main()
