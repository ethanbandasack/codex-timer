"""Export saved Codex usage history as a two-panel PNG chart."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from .history import default_data_dir


class ChartDependencyError(RuntimeError):
    """Raised when the optional Matplotlib dependency is missing."""


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
    return {"daily": daily, "quota": quotas, "days": history.get("days", 30)}


def default_export_path(now: dt.datetime | None = None) -> Path:
    now = now or dt.datetime.now().astimezone()
    name = f"codex-usage-{now.strftime('%Y%m%d-%H%M%S')}.png"
    return default_data_dir() / "exports" / name


def export_chart(history: dict[str, Any], output: Path) -> Path:
    try:
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import pyplot as plt
    except ImportError as exc:
        raise ChartDependencyError(
            "PNG export needs Matplotlib. Install it with: python3 -m pip install '.[charts]'"
        ) from exc

    series = chart_series(history)
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    figure, (token_axis, quota_axis) = plt.subplots(
        2,
        1,
        figsize=(11, 7),
        gridspec_kw={"height_ratios": (1, 1.35)},
        constrained_layout=True,
    )
    figure.suptitle(
        f"Codex usage history · last {series['days']} days", fontsize=15, fontweight="bold"
    )

    daily = series["daily"]
    if daily:
        dates, tokens = zip(*daily)
        token_axis.plot(dates, tokens, marker="o", markersize=4, linewidth=1.8, color="#3b82f6")
        token_axis.fill_between(dates, tokens, alpha=0.12, color="#3b82f6")
        token_axis.set_ylabel("Tokens")
        token_axis.set_title("Daily token activity reported by Codex", loc="left", fontsize=10)
        token_axis.ticklabel_format(axis="y", style="plain")
    else:
        token_axis.text(0.5, 0.5, "No daily token activity returned yet", ha="center", va="center")
        token_axis.set_title("Daily token activity reported by Codex", loc="left", fontsize=10)
        token_axis.set_yticks([])

    for label, color in (("5-hour", "#0f766e"), ("weekly", "#d97706")):
        points = series["quota"][label]
        if points:
            timestamps, percentages = zip(*points)
            quota_axis.plot(
                timestamps,
                percentages,
                label=label,
                linewidth=1.5,
                color=color,
                alpha=0.9,
            )
    if any(series["quota"].values()):
        quota_axis.legend(frameon=False, loc="upper left")
    else:
        quota_axis.text(0.5, 0.5, "No quota snapshots recorded yet", ha="center", va="center")
    quota_axis.set_title("Quota consumption snapshots", loc="left", fontsize=10)
    quota_axis.set_ylabel("Used (%)")
    quota_axis.set_ylim(0, 100)

    for axis in (token_axis, quota_axis):
        axis.grid(axis="y", color="#d1d5db", linewidth=0.7, alpha=0.7)
        axis.spines[["top", "right"]].set_visible(False)
    quota_axis.tick_params(axis="x", labelrotation=20)

    figure.savefig(output, dpi=160, facecolor="white")
    plt.close(figure)
    return output
