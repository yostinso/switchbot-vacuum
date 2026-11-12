"""Decode and render the K10+ (Qihoo 360 platform) map in an executor.

The map record carries an occupancy grid as base64(LZ4 block) with
``width``/``height``/``resolution``/``x_min``/``y_min`` plus world-space room
polygons (``smartArea.value[].vertexs``) and the dock (``chargeHandlePos``).
The grid layout matches independently reverse-engineered 360 robots
(0 = unknown, 127 = floor, 255 = wall). Units are checked against the room
polygons rather than assumed, and the chosen interpretation is reported.

Pure standard library: no Pillow/lz4 dependency.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import struct
import zlib
from collections import Counter
from typing import Any

MAX_CELLS = 4_000_000
MAX_IMAGE_EDGE = 1600
MARGIN_CELLS = 3
# The SwitchBot app shows K10+ maps rotated 180° relative to the raw grid (verified on hardware).
ROTATION = 180
C_FLOOR = (200, 204, 212, 255)  # Floor not assigned to any room.
C_WALL = (66, 60, 100, 255)  # Walls and obstacles.
C_UNKNOWN = (0, 0, 0, 0)  # Unexplored: transparent.
C_NOGO = (224, 74, 74)
C_DOCK = (30, 120, 210)
C_ROBOT = (235, 126, 24)
C_PATH = (255, 255, 255, 220)  # RGBA; alpha is the blend weight.
C_LABEL_BG = (44, 46, 62, 165)
# Saturated, app-like room colours; red is left for no-go zones.
ROOM_COLORS = (
    (126, 186, 240),
    (246, 168, 92),
    (150, 146, 232),
    (244, 196, 112),
    (110, 212, 178),
    (236, 136, 112),
    (100, 160, 228),
    (196, 150, 220),
)

# (vertex units per grid unit, label). Grid origin/resolution are used as given.
# 1000: vertices/dock in mm, origin and resolution in metres (360 convention).
# 1: everything in the same unit.
_HYPOTHESES = ((1000.0, "vertex_mm_origin_m"), (1.0, "same_units"))


def lz4_block_decompress(src: bytes, out_size: int) -> bytes:
    """Bounds-checked LZ4 block decompression."""
    out = bytearray()
    si, n = 0, len(src)
    while si < n:
        token = src[si]
        si += 1
        literal = token >> 4
        if literal == 15:
            while True:
                if si >= n:
                    raise ValueError("Truncated LZ4 literal length")
                b = src[si]
                si += 1
                literal += b
                if b != 255:
                    break
        if si + literal > n or len(out) + literal > out_size:
            raise ValueError("LZ4 literal overruns buffer")
        out += src[si : si + literal]
        si += literal
        if si >= n:
            break
        if si + 2 > n:
            raise ValueError("Truncated LZ4 offset")
        offset = src[si] | (src[si + 1] << 8)
        si += 2
        match = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                if si >= n:
                    raise ValueError("Truncated LZ4 match length")
                b = src[si]
                si += 1
                match += b
                if b != 255:
                    break
        start = len(out) - offset
        if offset == 0 or start < 0 or len(out) + match > out_size:
            raise ValueError("Invalid LZ4 match")
        for k in range(match):  # Overlapping copies must be byte-by-byte.
            out.append(out[start + k])
    return bytes(out)


def decode_grid(map_data: dict[str, Any]) -> tuple[int, int, bytes]:
    width, height = int(map_data["width"]), int(map_data["height"])
    if not 0 < width * height <= MAX_CELLS:
        raise ValueError("Invalid map dimensions")
    raw = map_data["map"]
    packed = base64.b64decode(raw, validate=True) if isinstance(raw, str) else bytes(raw)
    if len(packed) == width * height:
        grid = packed  # Some firmware may upload the grid uncompressed.
    else:
        grid = lz4_block_decompress(packed, width * height)
    if len(grid) != width * height:
        raise ValueError("Occupancy grid size does not match its dimensions")
    return width, height, grid


def _png_rgba(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + bytes(row) for row in rows), 6))
        + chunk(b"IEND", b"")
    )


def _name(value: Any, fallback: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode("utf-8") or fallback
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError):
        return value if isinstance(value, str) and value else fallback


def _smart_area(map_data: dict[str, Any]) -> dict[str, Any]:
    area = map_data.get("smartArea") or {}
    area = json.loads(area) if isinstance(area, str) else area
    return area if isinstance(area, dict) else {}


def _points(value: Any) -> list[tuple[float, float]]:
    """Accept [[x, y], ...], [x1, y1, x2, y2, ...], [{x, y}, ...] or a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    if value and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        value = [value[i : i + 2] for i in range(0, len(value) - 1, 2)]
    points = []
    for p in value:
        if isinstance(p, dict):
            p = [p.get("x", p.get("X")), p.get("y", p.get("Y"))]
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                x, y = float(p[0]), float(p[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(x) and math.isfinite(y):
                points.append((x, y))
    return points


class _Frame:
    """World (vertex units) <-> grid cell <-> cropped, scaled, rotated image pixels."""

    def __init__(self, x_min, y_min, res, height, factor, crop, scale, rotation=0):
        self.x_min, self.y_min, self.res = x_min, y_min, res
        self.height, self.factor, self.scale = height, factor, scale
        self.left, self.top, self.right, self.bottom = crop  # top-origin rows
        self.rotation = rotation
        self.image_w = (self.right - self.left + 1) * scale
        self.image_h = (self.bottom - self.top + 1) * scale

    def cell(self, x: float, y: float) -> tuple[float, float]:
        """Continuous grid coordinates, rows counted from the bottom."""
        return (x / self.factor - self.x_min) / self.res, (y / self.factor - self.y_min) / self.res

    def crop(self, x: float, y: float) -> tuple[float, float]:
        """Unrotated, unscaled crop-space coordinates (rows from the top)."""
        cx, cy = self.cell(x, y)
        return cx - self.left, self.height - cy - self.top

    def to_pixel(self, ccx: float, ccy: float) -> tuple[float, float]:
        px, py = ccx * self.scale, ccy * self.scale
        if self.rotation == 180:
            return self.image_w - px, self.image_h - py
        return px, py

    def pixel(self, x: float, y: float) -> tuple[float, float]:
        return self.to_pixel(*self.crop(x, y))

    def world(self, px: float, py: float) -> tuple[float, float]:
        if self.rotation == 180:
            px, py = self.image_w - px, self.image_h - py
        cx = px / self.scale + self.left
        cy = self.height - (py / self.scale + self.top)
        return (cx * self.res + self.x_min) * self.factor, (cy * self.res + self.y_min) * self.factor

    def as_tuple(self) -> tuple:
        return (
            self.x_min, self.y_min, self.res, self.height, self.factor,
            (self.left, self.top, self.right, self.bottom), self.scale, self.rotation,
        )


def _choose_factor(rooms, dock, x_min, y_min, res, width, height) -> tuple[float, str, float]:
    """Pick the unit interpretation that puts room/dock points on the grid."""
    points = [p for room in rooms for p in room["vertexs"]] + ([dock] if dock else [])
    if not points:
        return _HYPOTHESES[0][0], _HYPOTHESES[0][1], 0.0
    scored = []
    for factor, label in _HYPOTHESES:
        cells = [((x / factor - x_min) / res, (y / factor - y_min) / res) for x, y in points]
        fit = sum(0 <= cx <= width and 0 <= cy <= height for cx, cy in cells) / len(cells)
        # Wrong units collapse every polygon into a cell or two; prefer real extent.
        extent = max(
            max(c[0] for c in cells) - min(c[0] for c in cells),
            max(c[1] for c in cells) - min(c[1] for c in cells),
        )
        scored.append(((round(fit, 2), min(extent, width + height)), factor, label, fit))
    _, factor, label, fit = max(scored, key=lambda s: s[0])
    return factor, label, fit


def _fill_polygon(cells: list[tuple[float, float]], cw: int, ch: int):
    """Yield (col, row) cells whose centres are inside a polygon in crop space."""
    for row in range(max(0, int(min(p[1] for p in cells))), min(ch, math.ceil(max(p[1] for p in cells)) + 1)):
        scan = row + 0.5
        xs = sorted(
            a[0] + (scan - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            for a, b in zip(cells, cells[1:] + cells[:1])
            if (a[1] <= scan < b[1]) or (b[1] <= scan < a[1])
        )
        for left, right in zip(xs[::2], xs[1::2]):
            for col in range(max(0, math.ceil(left - 0.5)), min(cw, math.ceil(right - 0.5))):
                yield col, row


def _disc(rows, width, height, x, y, radius, color, ring=None):
    for yy in range(max(0, int(y - radius - 1)), min(height, int(y + radius + 2))):
        row = rows[yy]
        for xx in range(max(0, int(x - radius - 1)), min(width, int(x + radius + 2))):
            d = math.hypot(xx + 0.5 - x, yy + 0.5 - y)
            if d <= radius:
                rgb = color if ring is None or d <= radius - 2 else ring
                row[xx * 4 : xx * 4 + 4] = bytes((*rgb, 255))


def _classify_grid(grid: bytes, width: int, height: int) -> tuple[int, int | None]:
    """Return (background, floor) cell values; everything else is wall/obstacle.

    Robots disagree on the encoding (K10+: 127 unexplored, 255 floor, 0 walls;
    other 360 models: 0 unexplored, 127 floor, 255 walls). The value filling the
    grid's outer border is unexplored space; the commoner remaining value is floor.
    """
    border = Counter(grid[:width]) + Counter(grid[(height - 1) * width :])
    for r in range(1, height - 1):
        border[grid[r * width]] += 1
        border[grid[r * width + width - 1]] += 1
    background = border.most_common(1)[0][0]
    others = Counter(grid)
    del others[background]
    return background, (others.most_common(1)[0][0] if others else None)


def _stroke(rows, width, height, segments, rgba, half) -> None:
    """Blend line segments (pixel coordinates) of half-width ``half`` onto rows."""
    painted: set[tuple[int, int]] = set()
    for (x0, y0), (x1, y1) in segments:
        steps = max(1, int(math.hypot(x1 - x0, y1 - y0) * 2))
        for i in range(steps + 1):
            x = x0 + (x1 - x0) * i / steps
            y = y0 + (y1 - y0) * i / steps
            for yy in range(int(y - half), int(y + half) + 1):
                for xx in range(int(x - half), int(x + half) + 1):
                    if 0 <= xx < width and 0 <= yy < height:
                        painted.add((xx, yy))
    r, g, b, a = rgba
    for xx, yy in painted:
        px = rows[yy][xx * 4 : xx * 4 + 4]
        if px[3]:
            rows[yy][xx * 4 : xx * 4 + 4] = bytes((
                (px[0] * (255 - a) + r * a) // 255,
                (px[1] * (255 - a) + g * a) // 255,
                (px[2] * (255 - a) + b * a) // 255,
                255,
            ))
        else:
            rows[yy][xx * 4 : xx * 4 + 4] = bytes((r, g, b, a))


def _draw_labels(rows, width, height, labels, scale) -> list:
    """Room names in rounded pills, like the app; skipped if Pillow is unavailable."""
    if not labels:
        return rows
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return rows
    image = Image.frombytes("RGBA", (width, height), b"".join(bytes(r) for r in rows))
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.load_default(size=max(10, scale * 5))
    except TypeError:  # Pillow < 10.1 has no scalable default font.
        font = ImageFont.load_default()
    pad = max(2, scale)
    for text, x, y in labels:
        left, top, right, bottom = draw.textbbox((x, y), text, font=font, anchor="mm")
        draw.rounded_rectangle(
            (left - pad * 2, top - pad, right + pad * 2, bottom + pad),
            radius=(bottom - top) // 2 + pad, fill=C_LABEL_BG,
        )
        draw.text((x, y), text, font=font, fill=(255, 255, 255, 255), anchor="mm")
    data = Image.alpha_composite(image, overlay).tobytes()
    stride = width * 4
    return [bytearray(data[i * stride : (i + 1) * stride]) for i in range(height)]


def decode_k10_map(map_data: dict[str, Any]) -> dict[str, Any]:
    """Return rooms, geometry and a rendered PNG for a K10+ map record."""
    area = _smart_area(map_data)
    rooms = []
    for room in area.get("value") or []:
        if isinstance(room, dict) and room.get("id") is not None:
            rooms.append({
                "id": str(room["id"]),
                "name": _name(room.get("name"), f"Room {room['id']}"),
                "vertexs": _points(room.get("vertexs")),
            })
    room_names = {f"room{r['id']}": r["name"] for r in rooms}
    dock_points = _points([map_data.get("chargeHandlePos")])
    dock = dock_points[0] if dock_points else None

    width, height, grid = decode_grid(map_data)
    res = float(map_data["resolution"])
    x_min, y_min = float(map_data.get("x_min", 0)), float(map_data.get("y_min", 0))
    if not (res > 0 and math.isfinite(res) and math.isfinite(x_min) and math.isfinite(y_min)):
        raise ValueError("Invalid map transform")
    factor, hypothesis, fit = _choose_factor(rooms, dock, x_min, y_min, res, width, height)

    background, floor_value = _classify_grid(grid, width, height)

    # Crop to known cells (image rows are top-origin; grid row 0 is the bottom).
    cols, rows_known = [], []
    for r in range(height):
        line = grid[r * width : (r + 1) * width]
        if line.count(background) != width:
            rows_known.append(height - 1 - r)
            first = next(i for i, v in enumerate(line) if v != background)
            last = width - 1 - next(i for i, v in enumerate(reversed(line)) if v != background)
            cols += (first, last)
    # No-go zones may extend over unexplored space; keep them whole, as the app does.
    for zone in map_data.get("area") or []:
        for x, y in _points(zone.get("vertexs")) if isinstance(zone, dict) else []:
            cx, cy = (x / factor - x_min) / res, (y / factor - y_min) / res
            if 0 <= cx < width and 0 <= cy < height:
                cols.append(int(cx))
                rows_known.append(height - 1 - int(cy))
    if not cols:
        left, top, right, bottom = 0, 0, width - 1, height - 1
    else:
        left = max(0, min(cols) - MARGIN_CELLS)
        right = min(width - 1, max(cols) + MARGIN_CELLS)
        top = max(0, min(rows_known) - MARGIN_CELLS)
        bottom = min(height - 1, max(rows_known) + MARGIN_CELLS)
    cw, ch = right - left + 1, bottom - top + 1
    scale = max(1, min(4, MAX_IMAGE_EDGE // max(cw, ch)))
    frame = _Frame(
        x_min, y_min, res, height, factor, (left, top, right, bottom), scale, ROTATION
    )

    # Paint cells, then expand. cell_rgba[row][col] in crop space.
    cells = [[C_UNKNOWN] * cw for _ in range(ch)]
    floor = [[False] * cw for _ in range(ch)]
    for row in range(ch):
        source = (height - 1 - (top + row)) * width
        line = grid[source + left : source + left + cw]
        for col, value in enumerate(line):
            if value == background:
                continue
            if value == floor_value:
                cells[row][col] = C_FLOOR
                floor[row][col] = True
            else:
                cells[row][col] = C_WALL

    crop_cell = frame.crop

    def to_pixel(ccx: float, ccy: float) -> list[float]:
        px, py = frame.to_pixel(ccx, ccy)
        return [round(px, 1), round(py, 1)]

    geometry = {}
    room_stats = []
    labels: list[tuple[str, float, float]] = []
    for index, room in enumerate(rooms):
        if len(room["vertexs"]) < 3:
            room_stats.append({"vertices": len(room["vertexs"]), "skipped": True})
            continue
        polygon = [crop_cell(x, y) for x, y in room["vertexs"]]
        color = (*ROOM_COLORS[index % len(ROOM_COLORS)], 255)
        interior = []
        under: Counter = Counter()
        for col, row in _fill_polygon(polygon, cw, ch):
            under[grid[(height - 1 - (top + row)) * width + left + col]] += 1
            if floor[row][col]:
                cells[row][col] = color
                interior.append((col + 0.5, row + 0.5))
        room_stats.append({
            "vertices": len(polygon),
            "crop_bbox": [
                round(min(p[0] for p in polygon), 1), round(min(p[1] for p in polygon), 1),
                round(max(p[0] for p in polygon), 1), round(max(p[1] for p in polygon), 1),
            ],
            "cells_inside": sum(under.values()),
            "cells_coloured": len(interior),
            "grid_values_inside": {str(v): c for v, c in under.most_common(5)},
        })
        if interior:
            # Centre of the room's floor, snapped onto its floor for L/U shapes.
            cx = sum(p[0] for p in interior) / len(interior)
            cy = sum(p[1] for p in interior) / len(interior)
            cx, cy = min(interior, key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        else:
            cx = sum(p[0] for p in polygon) / len(polygon)
            cy = sum(p[1] for p in polygon) / len(polygon)
        labels.append((room["name"], *frame.to_pixel(cx, cy)))
        geometry[f"room{room['id']}"] = {
            "name": room["name"],
            "outline": [to_pixel(x, y) for x, y in polygon],
            "outline_vacuum": [[round(x), round(y)] for x, y in room["vertexs"]],
            "x": to_pixel(cx, cy)[0],
            "y": to_pixel(cx, cy)[1],
        }

    zone_outlines = []
    for zone in map_data.get("area") or []:
        vertexs = _points(zone.get("vertexs")) if isinstance(zone, dict) else []
        if len(vertexs) >= 3:
            for col, row in _fill_polygon([crop_cell(x, y) for x, y in vertexs], cw, ch):
                base = cells[row][col]
                if base[3]:
                    cells[row][col] = tuple(
                        (b * 3 + n * 2) // 5 for b, n in zip(base[:3], C_NOGO)
                    ) + (255,)
                else:  # Zones may extend over unexplored space, as in the app.
                    cells[row][col] = (*C_NOGO, 90)
            outline = [frame.pixel(x, y) for x, y in vertexs]
            zone_outlines += list(zip(outline, outline[1:] + outline[:1]))

    image_w, image_h = frame.image_w, frame.image_h
    pixel_rows = []
    for row in (cells[::-1] if ROTATION == 180 else cells):
        row = row[::-1] if ROTATION == 180 else row
        expanded = b"".join(bytes(c) * scale for c in row)
        pixel_rows.extend(bytearray(expanded) for _ in range(scale))
    _stroke(pixel_rows, image_w, image_h, zone_outlines, (*C_NOGO, 255), max(0.5, scale / 4))

    charger = None
    if dock:
        px, py = frame.pixel(*dock)
        if 0 <= px < image_w and 0 <= py < image_h:
            _disc(pixel_rows, image_w, image_h, px, py, max(5, scale * 2), C_DOCK, (255, 255, 255))
            charger = {"x": round(px, 1), "y": round(py, 1)}

    base_image = _png_rgba(
        image_w, image_h, _draw_labels(pixel_rows, image_w, image_h, labels, scale)
    )
    attributes = {
        "map_id": str(map_data.get("mapId", area.get("mapId", ""))),
        "rooms": geometry,
        "charger": charger,
        "width": image_w,
        "height": image_h,
        "resolution": res,
        "calibration_points": _calibration(frame),
        "robot_position_available": False,
        "robot_position": None,
        "route_available": False,
        "coordinate_fit": {"hypothesis": hypothesis, "points_on_grid": round(fit, 3)},
        "source": "SwitchBot K10+ cloud map",
    }
    if dock:
        attributes["charger_position"] = {"x": round(dock[0]), "y": round(dock[1])}
    return {
        "_base_image": base_image,
        "_render_rows": [bytes(r) for r in pixel_rows],
        "_frame": frame.as_tuple(),
        "_labels": labels,
        "_room_stats": {
            "crop_size": [cw, ch],
            "grid_classes": {"background": background, "floor": floor_value},
            "rooms": room_stats,
        },
        "rooms": room_names,
        "image": base_image,
        "attributes": attributes,
    }


def _calibration(frame: _Frame) -> list[dict[str, dict[str, float]]]:
    """Three vacuum<->image point pairs, as used by xiaomi-vacuum-map-card."""
    out = []
    for px, py in ((0, 0), (100, 0), (0, 100)):
        vx, vy = frame.world(px, py)
        out.append({"vacuum": {"x": round(vx, 1), "y": round(vy, 1)}, "map": {"x": px, "y": py}})
    return out


def parse_pose(value: Any) -> tuple[float, float, float] | None:
    """Parse CurrentLocation: [x, y, heading] (string or list)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        value = [value.get("x"), value.get("y"), value.get("angle", value.get("a", 0))]
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        pose = tuple(float(v) for v in (list(value) + [0])[:3])
    except (TypeError, ValueError):
        return None
    return pose if all(math.isfinite(v) for v in pose) else None


def parse_path(value: Any) -> list[tuple[float, float]]:
    """Track points ``[[x, y, ...], ...]`` in robot units; tolerant like rooms."""
    return _points(value)


def _draw_path(rows, frame: _Frame, path: list[tuple[float, float]]) -> int:
    """Blend the cleaning path onto the pixels; return the number of segments drawn."""
    max_jump = 1.0 * frame.factor  # Skip gaps over 1 m (relocalisation, missing samples).
    segments = [
        (frame.pixel(ax, ay), frame.pixel(bx, by))
        for (ax, ay), (bx, by) in zip(path, path[1:])
        if math.hypot(bx - ax, by - ay) <= max_jump
    ]
    _stroke(rows, frame.image_w, frame.image_h, segments, C_PATH, max(0.5, frame.scale / 4))
    return len(segments)


def render_k10_overlay(
    map_data: dict[str, Any],
    pose: tuple[float, float, float] | None = None,
    path: list[tuple[float, float]] | None = None,
) -> dict:
    """Draw the last cleaning path and the robot on a copy of the cached pixels."""
    frame = _Frame(*map_data["_frame"])
    width, height = frame.image_w, frame.image_h
    rows = [bytearray(r) for r in map_data["_render_rows"]]
    result: dict[str, Any] = {"pixel": None, "path_segments": 0}
    if path:
        result["path_segments"] = _draw_path(rows, frame, path)
    rows = _draw_labels(rows, width, height, map_data.get("_labels") or [], frame.scale)
    if pose is not None:
        x, y = frame.pixel(pose[0], pose[1])
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError("Pose outside map")
        radius = max(6, frame.scale * 2.5)
        _disc(rows, width, height, x, y, radius, C_ROBOT, (255, 255, 255))
        # Heading: degrees (or radians if small), counter-clockwise in the robot frame.
        angle = math.radians(pose[2]) if abs(pose[2]) > 2 * math.pi + 0.01 else pose[2]
        step = frame.res * frame.factor
        tx, ty = frame.pixel(pose[0] + math.cos(angle) * step, pose[1] + math.sin(angle) * step)
        norm = math.hypot(tx - x, ty - y) or 1
        dx, dy = (tx - x) / norm, (ty - y) / norm
        for i in range(int(radius * 1.8)):
            xx, yy = round(x + dx * i), round(y + dy * i)
            if 0 <= xx < width and 0 <= yy < height:
                rows[yy][xx * 4 : xx * 4 + 4] = bytes((120, 54, 5, 255))
        result["pixel"] = {"x": round(x, 1), "y": round(y, 1)}
    result["image"] = _png_rgba(width, height, rows)
    return result


def render_k10_position(map_data: dict[str, Any], pose: tuple[float, float, float]) -> dict:
    """Draw only the robot (kept for callers without a path)."""
    return render_k10_overlay(map_data, pose)


def describe_map(map_data: dict[str, Any], record_count: int = 0) -> dict[str, Any]:
    """Structure-only summary for diagnostics: no names, URLs or identifiers."""

    def shape(value: Any) -> str:
        if isinstance(value, (list, str, dict)):
            return f"{type(value).__name__}[{len(value)}]"
        return type(value).__name__

    summary: dict[str, Any] = {
        "record_count": record_count,
        "keys": {k: shape(v) for k, v in sorted(map_data.items())},
    }
    for key in ("width", "height", "resolution", "x_min", "y_min", "x_max", "y_max"):
        if isinstance(map_data.get(key), (int, float)):
            summary[key] = map_data[key]
    area = _smart_area(map_data)
    summary["smart_area_keys"] = sorted(area)
    room_shapes = []
    for room in area.get("value") or []:
        if isinstance(room, dict):
            pts = _points(room.get("vertexs"))
            room_shapes.append({
                "keys": sorted(room),
                "vertex_count": len(pts),
                "x_range": [round(min(p[0] for p in pts)), round(max(p[0] for p in pts))] if pts else None,
                "y_range": [round(min(p[1] for p in pts)), round(max(p[1] for p in pts))] if pts else None,
            })
    summary["rooms"] = room_shapes
    dock = _points([map_data.get("chargeHandlePos")])
    summary["dock_present"] = bool(dock)
    if "map" in map_data and "width" in map_data:
        try:
            width, height, grid = decode_grid(map_data)
            summary["grid"] = {
                "decoded": True,
                "values": {str(v): c for v, c in Counter(grid).most_common(12)},
            }
        except (ValueError, TypeError, KeyError, binascii.Error) as exc:
            summary["grid"] = {"decoded": False, "error": type(exc).__name__}
    return summary
