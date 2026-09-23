"""Render saved Codex usage history as a PNG using only the Python standard library."""

from __future__ import annotations

import datetime as dt
import struct
import zlib
from pathlib import Path
from typing import Any

from .history import default_data_dir


class ChartDependencyError(RuntimeError):
    """Compatibility exception retained for older callers; rendering is built in."""


_FONT = {
    "A": (14, 17, 17, 31, 17, 17, 17),
    "B": (30, 17, 17, 30, 17, 17, 30),
    "C": (14, 17, 16, 16, 16, 17, 14),
    "D": (30, 17, 17, 17, 17, 17, 30),
    "E": (31, 16, 16, 30, 16, 16, 31),
    "F": (31, 16, 16, 30, 16, 16, 16),
    "G": (14, 17, 16, 23, 17, 17, 15),
    "H": (17, 17, 17, 31, 17, 17, 17),
    "I": (14, 4, 4, 4, 4, 4, 14),
    "J": (7, 2, 2, 2, 18, 18, 12),
    "K": (17, 18, 20, 24, 20, 18, 17),
    "L": (16, 16, 16, 16, 16, 16, 31),
    "M": (17, 27, 21, 21, 17, 17, 17),
    "N": (17, 25, 21, 19, 17, 17, 17),
    "O": (14, 17, 17, 17, 17, 17, 14),
    "P": (30, 17, 17, 30, 16, 16, 16),
    "Q": (14, 17, 17, 17, 21, 18, 13),
    "R": (30, 17, 17, 30, 20, 18, 17),
    "S": (15, 16, 16, 14, 1, 1, 30),
    "T": (31, 4, 4, 4, 4, 4, 4),
    "U": (17, 17, 17, 17, 17, 17, 14),
    "V": (17, 17, 17, 17, 17, 10, 4),
    "W": (17, 17, 17, 21, 21, 21, 10),
    "X": (17, 17, 10, 4, 10, 17, 17),
    "Y": (17, 17, 10, 4, 4, 4, 4),
    "Z": (31, 1, 2, 4, 8, 16, 31),
    "0": (14, 17, 19, 21, 25, 17, 14),
    "1": (4, 12, 4, 4, 4, 4, 14),
    "2": (14, 17, 1, 2, 4, 8, 31),
    "3": (30, 1, 1, 14, 1, 1, 30),
    "4": (2, 6, 10, 18, 31, 2, 2),
    "5": (31, 16, 16, 30, 1, 1, 30),
    "6": (14, 16, 16, 30, 17, 17, 14),
    "7": (31, 1, 2, 4, 8, 8, 8),
    "8": (14, 17, 17, 14, 17, 17, 14),
    "9": (14, 17, 17, 15, 1, 1, 14),
    ".": (0, 0, 0, 0, 0, 12, 12),
    ":": (0, 12, 12, 0, 12, 12, 0),
    "-": (0, 0, 0, 31, 0, 0, 0),
    "/": (1, 2, 2, 4, 8, 8, 16),
    "%": (17, 2, 4, 8, 17, 0, 0),
    "+": (0, 4, 4, 31, 4, 4, 0),
    "(": (2, 4, 8, 8, 8, 4, 2),
    ")": (8, 4, 2, 2, 2, 4, 8),
    " ": (0, 0, 0, 0, 0, 0, 0),
}


def chart_series(history: dict[str, Any]) -> dict[str, Any]:
    """Convert stored rows into date/token and timestamp/quota series."""
    daily = [
        (dt.date.fromisoformat(row["usage_date"]), int(row["tokens"]))
        for row in history.get("daily", [])
    ]
    quotas: dict[str, list[tuple[dt.datetime, float]]] = {"5-hour": [], "weekly": []}
    for row in history.get("quota", []):
        label = row.get("window")
        if label not in quotas or row.get("used_percent") is None:
            continue
        timestamp = dt.datetime.fromtimestamp(float(row["captured_at"])).astimezone()
        quotas[label].append((timestamp, float(row["used_percent"])))
    for values in quotas.values():
        values.sort(key=lambda item: item[0])
    daily.sort(key=lambda item: item[0])
    return {"daily": daily, "quota": quotas, "days": history.get("days", 30)}


def default_export_path(now: dt.datetime | None = None) -> Path:
    now = now or dt.datetime.now().astimezone()
    name = f"codex-usage-{now.strftime('%Y%m%d-%H%M%S')}.png"
    return default_data_dir() / "exports" / name


class _Canvas:
    def __init__(self, width: int, height: int, background: tuple[int, int, int]) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(background * (width * height))

    def pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset : offset + 3] = bytes(color)

    def line(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
        thickness: int = 1,
    ) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx + dy
        radius = max(0, thickness // 2)
        while True:
            for ox in range(-radius, radius + 1):
                for oy in range(-radius, radius + 1):
                    self.pixel(x0 + ox, y0 + oy, color)
            if x0 == x1 and y0 == y1:
                break
            twice = 2 * error
            if twice >= dy:
                error += dy
                x0 += sx
            if twice <= dx:
                error += dx
                y0 += sy

    def circle(self, cx: int, cy: int, radius: int, color: tuple[int, int, int]) -> None:
        for y in range(-radius, radius + 1):
            for x in range(-radius, radius + 1):
                if x * x + y * y <= radius * radius:
                    self.pixel(cx + x, cy + y, color)

    def text(
        self,
        x: int,
        y: int,
        value: str,
        color: tuple[int, int, int],
        scale: int = 1,
    ) -> None:
        cursor = x
        for char in value.upper():
            glyph = _FONT.get(char, _FONT[" "])
            for row, bits in enumerate(glyph):
                for col in range(5):
                    if bits & (1 << (4 - col)):
                        for sy in range(scale):
                            for sx in range(scale):
                                self.pixel(
                                    cursor + col * scale + sx,
                                    y + row * scale + sy,
                                    color,
                                )
            cursor += 6 * scale

    def png(self) -> bytes:
        raw = b"".join(
            b"\x00" + self.pixels[row * self.width * 3 : (row + 1) * self.width * 3]
            for row in range(self.height)
        )

        def chunk(kind: bytes, data: bytes) -> bytes:
            payload = kind + data
            return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, level=7))
            + chunk(b"IEND", b"")
        )


def _nice_max(value: int) -> int:
    if value <= 0:
        return 1
    magnitude = 10 ** (len(str(value)) - 1)
    for factor in (1, 2, 5, 10):
        candidate = factor * magnitude
        if candidate >= value:
            return candidate
    return 10 * magnitude


def _draw_axes(
    canvas: _Canvas,
    bounds: tuple[int, int, int, int],
    max_y: int,
    formatter: Any,
) -> tuple[int, int, int, int]:
    left, top, right, bottom = bounds
    grid, axis, label = (229, 234, 240), (148, 163, 184), (100, 116, 139)
    for step in range(5):
        y = bottom - (bottom - top) * step // 4
        canvas.line(left, y, right, y, grid)
        canvas.text(18, y - 3, formatter(max_y * step // 4), label)
    canvas.line(left, top, left, bottom, axis)
    canvas.line(left, bottom, right, bottom, axis)
    return left, top, right, bottom


def _map_point(
    index: int,
    count: int,
    value: float,
    max_value: float,
    bounds: tuple[int, int, int, int],
) -> tuple[int, int]:
    left, top, right, bottom = bounds
    x = left if count <= 1 else left + round((right - left) * index / (count - 1))
    y = bottom - round((bottom - top) * min(max(value, 0), max_value) / max_value)
    return x, y


def _render_chart(series: dict[str, Any]) -> bytes:
    canvas = _Canvas(1200, 760, (250, 251, 253))
    ink, muted = (30, 41, 59), (100, 116, 139)
    blue, teal, orange = (59, 130, 246), (15, 118, 110), (217, 119, 6)
    canvas.text(34, 30, "CODEX USAGE HISTORY", ink, scale=2)
    canvas.text(36, 55, f"LAST {series['days']} DAYS · LOCAL HISTORY", muted)

    canvas.text(72, 100, "DAILY TOKEN ACTIVITY", ink, scale=1)
    daily = series["daily"]
    token_max = _nice_max(max((tokens for _, tokens in daily), default=1))
    token_bounds = _draw_axes(canvas, (104, 132, 1150, 306), token_max, lambda n: str(n))
    if daily:
        points = [
            _map_point(index, len(daily), tokens, token_max, token_bounds)
            for index, (_, tokens) in enumerate(daily)
        ]
        for first, second in zip(points, points[1:]):
            canvas.line(*first, *second, blue, thickness=3)
        for point in points:
            canvas.circle(*point, 4, blue)
        for index in sorted({0, len(daily) // 2, len(daily) - 1}):
            x, _ = _map_point(index, len(daily), 0, token_max, token_bounds)
            label = daily[index][0].strftime("%b %d").upper()
            canvas.text(max(105, min(x - len(label) * 3, 1100)), 316, label, muted)
    else:
        canvas.text(400, 208, "NO DAILY TOKEN ACTIVITY REPORTED YET", muted)

    canvas.text(72, 382, "QUOTA CONSUMPTION", ink)
    quota_bounds = _draw_axes(canvas, (104, 414, 1150, 620), 100, lambda n: f"{n}%")
    all_quota = [point for values in series["quota"].values() for point in values]
    if all_quota:
        start = min(timestamp for timestamp, _ in all_quota).timestamp()
        finish = max(timestamp for timestamp, _ in all_quota).timestamp()
        span = max(1, finish - start)
        for label, color in (("5-hour", teal), ("weekly", orange)):
            values = series["quota"][label]
            points = []
            for timestamp, percent in values:
                x = quota_bounds[0] + round(
                    (quota_bounds[2] - quota_bounds[0]) * (timestamp.timestamp() - start) / span
                )
                y = quota_bounds[3] - round(
                    (quota_bounds[3] - quota_bounds[1]) * min(max(percent, 0), 100) / 100
                )
                points.append((x, y))
            for first, second in zip(points, points[1:]):
                canvas.line(*first, *second, color, thickness=3)
            for point in points:
                canvas.circle(*point, 4, color)
        first_date = dt.datetime.fromtimestamp(start).astimezone().strftime("%Y-%m-%d")
        last_date = dt.datetime.fromtimestamp(finish).astimezone().strftime("%Y-%m-%d")
        canvas.text(104, 632, first_date, muted)
        canvas.text(1050, 632, last_date, muted)
        canvas.line(470, 658, 496, 658, teal, thickness=3)
        canvas.text(504, 655, "5-HOUR", muted)
        canvas.line(620, 658, 646, 658, orange, thickness=3)
        canvas.text(654, 655, "WEEKLY", muted)
    else:
        canvas.text(430, 510, "NO QUOTA SNAPSHOTS RECORDED YET", muted)
    canvas.text(36, 720, "SOURCE: CODEX USAGE SNAPSHOTS SAVED LOCALLY", muted)
    return canvas.png()


def export_chart(history: dict[str, Any], output: Path) -> Path:
    """Write a two-panel token and quota chart without optional packages."""
    series = chart_series(history)
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_render_chart(series))
    return output
