"""Plain-text histograms for saved Codex usage history."""

from __future__ import annotations

import datetime as dt
from typing import Any


def _compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:.0f}k"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _bar(value: float, maximum: float, width: int) -> str:
    if value <= 0 or maximum <= 0:
        return ""
    filled = max(1, round(value / maximum * width))
    return "#" * min(width, filled)


def render_histogram(history: dict[str, Any], width: int = 80) -> list[str]:
    """Render daily token activity, falling back to recent 5-hour quota snapshots."""
    width = max(48, width)
    bar_width = min(36, width - 31)
    daily = history.get("daily") or []
    lines: list[str] = []

    if daily:
        daily = daily[-14:]
        maximum = max((int(row["tokens"]) for row in daily), default=0)
        lines.append(f"Daily token activity · last {history.get('days', 30)} days")
        lines.append("Date        Tokens    Usage")
        for row in daily:
            tokens = max(0, int(row["tokens"]))
            lines.append(
                f"{row['usage_date']:<10}  {_compact_number(tokens):>7}    "
                f"{_bar(tokens, maximum, bar_width)}"
            )
        lines.append("Token totals are reported by Codex and may update with a delay.")
        return lines

    quota = [row for row in history.get("quota", []) if row.get("window") == "5-hour"]
    quota = quota[-14:]
    if quota:
        maximum = 100.0
        lines.append("Recent 5-hour quota snapshots · token activity not available yet")
        lines.append("Time            Used      Quota")
        for row in quota:
            used = max(0.0, min(100.0, float(row.get("used_percent") or 0)))
            timestamp = float(row["captured_at"])
            local = dt.datetime.fromtimestamp(timestamp).astimezone()
            label = local.strftime("%d %b %H:%M")
            lines.append(f"{label:<15}  {used:5.1f}%    {_bar(used, maximum, bar_width)}")
        return lines

    lines.extend(
        (
            "No usage history recorded yet.",
            "Open Codex Timer and refresh usage to start collecting local history.",
        )
    )
    return lines


def render_hourly_histogram(
    history: dict[str, Any], width: int = 80, hours: int = 24, now: float | None = None
) -> list[str]:
    """Render per-hour token counts reconstructed from this device's session logs."""
    width = max(48, width)
    hours = min(168, max(1, hours))
    now = now if now is not None else dt.datetime.now().astimezone().timestamp()
    current_hour = int(now // 3600) * 3600
    first_hour = current_hour - (hours - 1) * 3600
    counts = {first_hour + index * 3600: 0 for index in range(hours)}
    for row in history.get("local_tokens", []):
        hour = int(float(row["captured_at"]) // 3600 * 3600)
        if hour in counts:
            counts[hour] += int(row.get("total_tokens") or 0)

    maximum = max(counts.values(), default=0)
    if maximum <= 0:
        return [
            f"Local session token activity · last {hours} hours",
            "No local token-count events found in this period.",
            "Start a Codex session to create local token-count events.",
        ]

    bar_width = min(36, width - 31)
    lines = [
        f"Local session token activity · last {hours} hours",
        "Hour             Tokens    Usage",
    ]
    for timestamp, tokens in counts.items():
        local = dt.datetime.fromtimestamp(timestamp).astimezone()
        label = local.strftime("%a %d %H:%M %Z")
        lines.append(
            f"{label:<16}  {_compact_number(tokens):>7}    {_bar(tokens, maximum, bar_width)}"
        )
    lines.append("Per-event token counts come from this device's local Codex session logs.")
    lines.append("They are separate from the server's quota percentage.")
    return lines
