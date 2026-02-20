# mitra_bot/services/ups/ups_graph.py
from __future__ import annotations

import math
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
from pydantic import BaseModel, ConfigDict, model_validator

matplotlib.use("Agg")  # headless-safe

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.dates import ConciseDateFormatter  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


class UPSGraphRowModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str

    @model_validator(mode="before")
    @classmethod
    def _normalize_ts(cls, value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("UPS graph row must be an object.")
        row = dict(value)
        ts = row.get("ts") or row.get("timestamp") or row.get("time")
        if not isinstance(ts, str) or not ts.strip():
            raise ValueError("UPS graph row is missing timestamp.")
        row["ts"] = ts.strip()
        return row


def _parse_to_local_naive(ts: str, tz_name: str) -> Optional[datetime]:
    """
    Parse stored UTC ISO -> convert to tz -> strip tzinfo so matplotlib shows local wall time.
    """
    if not ts:
        return None

    tzinfo = None
    if ZoneInfo is not None:
        try:
            tzinfo = ZoneInfo(tz_name)
        except Exception:
            tzinfo = None

    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        if tzinfo is not None:
            dt = dt.astimezone(tzinfo)
        else:
            dt = dt.astimezone(timezone.utc)

        return dt.replace(tzinfo=None)
    except Exception:
        return None


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return float("nan")


def _mean_std(series: List[float]):
    vals = [v for v in series if not math.isnan(v)]
    if len(vals) < 2:
        return None, None
    mean = sum(vals) / len(vals)
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
    return mean, std


def build_ups_status_graph(
    rows: List[Dict[str, Any]],
    *,
    hours: int,
    timezone_name: str = "UTC",
) -> Optional[BytesIO]:
    """
    Recreates the original Mitra UPS graph style:
      - Runtime (min)
      - Output (W)
      - Input (V)

    Expects (old log format):
      ts
      time_to_empty_s
      output_w
      input_v

    Also supports (newer formats):
      time_to_empty_seconds
      output.power / output_w
      input.voltage / input_v
    """
    if not rows:
        return None

    parsed: List[Tuple[datetime, Dict[str, Any]]] = []
    for r in rows:
        try:
            norm = UPSGraphRowModel.model_validate(r).model_dump(mode="json")
        except Exception:
            continue
        dt = _parse_to_local_naive(str(norm.get("ts", "")), timezone_name)
        if dt:
            parsed.append((dt, norm))

    if len(parsed) < 2:
        return None

    parsed.sort(key=lambda x: x[0])
    xs = [dt for dt, _ in parsed]

    def _get_output_w(row: Dict[str, Any]) -> float:
        if "output_w" in row:
            return _to_float(row.get("output_w"))
        outp = row.get("output") or {}
        if isinstance(outp, dict) and "power" in outp:
            return _to_float(outp.get("power"))
        return float("nan")

    def _get_input_v(row: Dict[str, Any]) -> float:
        if "input_v" in row:
            return _to_float(row.get("input_v"))
        inp = row.get("input") or {}
        if isinstance(inp, dict) and "voltage" in inp:
            return _to_float(inp.get("voltage"))
        if "input_voltage" in row:
            return _to_float(row.get("input_voltage"))
        return float("nan")

    def _get_tte_minutes(row: Dict[str, Any]) -> float:
        # Old log key
        if "time_to_empty_s" in row:
            return _to_float(row.get("time_to_empty_s")) / 60.0
        # Newer key
        if "time_to_empty_seconds" in row:
            return _to_float(row.get("time_to_empty_seconds")) / 60.0
        # Sometimes refactors used this
        if "time_to_empty" in row:
            return _to_float(row.get("time_to_empty")) / 60.0
        return float("nan")

    tte = [_get_tte_minutes(r) for _, r in parsed]
    out_w = [_get_output_w(r) for _, r in parsed]
    in_v = [_get_input_v(r) for _, r in parsed]

    # If everything is NaN, skip
    if (
        len([v for v in tte if not math.isnan(v)]) < 2
        and len([v for v in out_w if not math.isnan(v)]) < 2
        and len([v for v in in_v if not math.isnan(v)]) < 2
    ):
        logging.info("UPS graph: not enough numeric points to graph.")
        return None

    # ---- Exact original Discord Dark Theme Colors ----
    FIG_BG = "#2B2D31"
    AX_BG = "#313338"
    TEXT = "#DBDEE1"
    GRID = "#4E5058"
    ACCENT = "#5865F2"

    fig = plt.figure(figsize=(12, 7), dpi=220)
    fig.patch.set_facecolor(FIG_BG)

    ax1 = fig.add_subplot(311)
    ax2 = fig.add_subplot(312, sharex=ax1)
    ax3 = fig.add_subplot(313, sharex=ax1)

    for ax in (ax1, ax2, ax3):
        ax.set_facecolor(AX_BG)
        ax.tick_params(colors=TEXT, labelsize=9)
        ax.yaxis.label.set_color(TEXT)
        ax.xaxis.label.set_color(TEXT)
        ax.grid(True, color=GRID, alpha=0.25, linewidth=0.8)
        for spine in ax.spines.values():
            spine.set_color(GRID)

    def _plot(ax, y: List[float], label: str, units: str):
        ax.plot(xs, y, linewidth=2.2, color=ACCENT, alpha=0.95)

        mean, std = _mean_std(y)
        if mean is not None:
            ax.axhline(mean, linestyle="--", linewidth=1.2, color=TEXT, alpha=0.55)
            if std and std > 0:
                ax.axhspan(mean - std, mean + std, color=TEXT, alpha=0.08)

        ax.set_ylabel(f"{label}\n({units})", fontsize=10)

        vals = [v for v in y if not math.isnan(v)]
        if len(vals) >= 2:
            vmin, vmax = min(vals), max(vals)
            if vmin == vmax:
                pad = 1.0 if vmin == 0 else abs(vmin) * 0.05
                ax.set_ylim(vmin - pad, vmax + pad)

    _plot(ax1, tte, "Runtime", "min")
    _plot(ax2, out_w, "Output", "W")
    _plot(ax3, in_v, "Input", "V")

    # ---- Better time axis formatting (original) ----
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    formatter = ConciseDateFormatter(locator)
    ax3.xaxis.set_major_locator(locator)
    ax3.xaxis.set_major_formatter(formatter)

    for ax in (ax1, ax2):
        plt.setp(ax.get_xticklabels(), visible=False)

    label_tz = timezone_name if ZoneInfo is not None else "UTC"
    ax3.set_xlabel(f"Time ({label_tz}) | last {hours}h", fontsize=10)

    fig.suptitle("Mitra UPS Status", fontsize=14, fontweight="bold", color=TEXT)
    fig.tight_layout(rect=[0, 0, 1, 0.96])

    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=FIG_BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf
