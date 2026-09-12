"""Headless availability graphs. Gray represents missing observations, never uptime."""
from __future__ import annotations

import threading
import math
from datetime import datetime, timezone
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import matplotlib.dates as mdates
from matplotlib.patches import Patch
from matplotlib.lines import Line2D
from mitra_bot.services.graph_theme import FIG_BG, AX_BG, TEXT, GRID, ACCENT, style_axis

_LOCK = threading.Lock()
UP, DOWN, UNKNOWN = "#2da778", "#df5b62", "#6D6F78"


def metrics(rows, start, end):
    up = sum(r["up"] for r in rows)
    down = sum(r["down"] for r in rows)
    known = up + down
    return dict(availability=100*up/known if known else None,
                coverage=min(100, 100*known/max(1, end-start)), up=up, down=down,
                unknown=max(0, end-start-known))


def _date(ts):
    return mdates.date2num(datetime.fromtimestamp(ts, timezone.utc))


def _timeline(ax, rows, start, end, y=0, gateway=False):
    ax.broken_barh([(_date(start), (end-start)/86400)], (y-.3, .6), facecolors=UNKNOWN)
    up_bars, down_bars = [], []
    for r in rows:
        left, right = max(start, r["ts"]), min(end, r["ts"]+r.get("width",300))
        up = r["gateway_up"] if gateway else r["up"]
        down = r["gateway_down"] if gateway else r["down"]
        # Width represents duration. Within-bucket positions are approximate.
        scale = min(1, max(0, right-left)/max(1, up+down))
        if up:
            up_bars.append((_date(left), up*scale/86400))
        if down:
            down_bars.append((_date(left+up*scale), down*scale/86400))
    ax.broken_barh(up_bars, (y-.3,.6), facecolors=UP)
    ax.broken_barh(down_bars, (y-.3,.6), facecolors=DOWN)


def _compact(rows, width):
    grouped = {}
    for row in rows:
        bucket = int(row["ts"]//width)*width
        target = grouped.setdefault(bucket, dict(ts=bucket,width=width,up=0,down=0,gateway_up=0,gateway_down=0,rt=0,rc=0))
        for key in ("up","down","gateway_up","gateway_down"):
            target[key] += row[key]
        if row["rtt"] is not None:
            target["rt"] += row["rtt"]*row["up"]
            target["rc"] += row["up"]
    return [dict(**value, rtt=value["rt"]/value["rc"] if value["rc"] else None) for value in grouped.values()]


def render_history(series, observer, start, end, *, detail=False, restarts=()):
    """Pure renderer: caller snapshots SQLite data before running in a worker thread."""
    with _LOCK:
        width = max(300, math.ceil((end-start)/720/300)*300)
        series = {node:_compact(rows,width) for node,rows in series.items()}
        figure = Figure(figsize=(11, 6 if detail else max(3, 1.5+len(series)*.65)), layout="constrained")
        figure.set_facecolor(FIG_BG)
        FigureCanvasAgg(figure)
        if detail:
            subject, rows = next(iter(series.items()))
            axes = figure.subplots(3, 1, sharex=True, gridspec_kw={"height_ratios": [1, 1, 2]})
            _timeline(axes[0], rows, start, end)
            _timeline(axes[1], rows, start, end, gateway=True)
            axes[0].set_ylabel("Peer link")
            axes[1].set_ylabel("Discord")
            for ax in axes[:2]:
                ax.set_yticks([])
                ax.set_ylim(-.6, .6)
            by_bucket = {r["ts"]: r for r in rows}
            stamps = range(int(start//width)*width, int(end)+1, width)
            x, y = [], []
            for ts in stamps:
                row = by_bucket.get(ts)
                x.append(_date(ts+width/2))
                y.append(row["rtt"] if row and row["rtt"] is not None else float("nan"))
            axes[2].plot(x, y, color=ACCENT, linewidth=2.2, alpha=.95)
            axes[2].set_ylabel("Mean RTT (ms)")
            axes[2].set_ylim(bottom=0)
            for ts in restarts:
                for ax in axes:
                    ax.axvline(_date(ts), color="#8859b6", linestyle=":", alpha=.7)
            figure.suptitle(f"{subject} — observed by {observer}", fontsize=15)
        else:
            ax = figure.subplots()
            axes = [ax]
            for index, (subject, rows) in enumerate(series.items()):
                _timeline(ax, rows, start, end, y=index)
            ax.set_yticks(range(len(series)), list(series))
            ax.set_ylim(len(series)-.4, -.6)
            figure.suptitle(f"Server availability — observed by {observer}", fontsize=15)
        for ax in axes:
            ax.set_xlim(_date(start), _date(end))
            style_axis(ax, grid_axis="x")
        figure._suptitle.set_color(TEXT)
        figure._suptitle.set_fontsize(14)
        figure._suptitle.set_fontweight("bold")
        locator = mdates.AutoDateLocator(minticks=3, maxticks=8)
        axes[-1].xaxis.set_major_locator(locator)
        axes[-1].xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=timezone.utc))
        axes[-1].set_xlabel(f"UTC • {width//60}-minute duration buckets • gray = unknown coverage")
        legend = [Patch(color=UP, label="Reachable / connected"),
                  Patch(color=DOWN, label="Failed probe / disconnected"), Patch(color=UNKNOWN, label="Unknown")]
        if detail and restarts:
            legend.append(Line2D([0],[0], color="#8859b6", linestyle=":", label="Process restarted"))
        axes[0].legend(handles=legend, loc="upper center", bbox_to_anchor=(.5, 1.5 if detail else 1.10), ncol=4, fontsize=8,
                       facecolor=AX_BG, edgecolor=GRID, labelcolor=TEXT, framealpha=1)
        output = BytesIO()
        figure.savefig(output, format="png", dpi=220, facecolor=FIG_BG)
        output.seek(0)
        return output
