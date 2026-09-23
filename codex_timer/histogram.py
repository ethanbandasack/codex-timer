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
