"""Shared Discord dark theme for monitoring graphs."""

FIG_BG = "#2B2D31"
AX_BG = "#313338"
TEXT = "#DBDEE1"
GRID = "#4E5058"
ACCENT = "#5865F2"


def style_axis(ax, *, grid_axis="both"):
    ax.set_facecolor(AX_BG)
    ax.tick_params(colors=TEXT, labelsize=9)
    ax.yaxis.label.set_color(TEXT)
    ax.xaxis.label.set_color(TEXT)
    ax.xaxis.get_offset_text().set_color(TEXT)
    ax.yaxis.get_offset_text().set_color(TEXT)
    ax.grid(True, axis=grid_axis, color=GRID, alpha=0.25, linewidth=0.8)
    for spine in ax.spines.values():
        spine.set_color(GRID)
