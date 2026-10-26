"""
stripchart — VCM-Scanner-style "Chart vs. Time" view over a truck-mcp session.

Stacked lanes, several channels per lane; each channel keeps its own vertical
scale, drawn as a color-matched min/mid/max column on the right (the strip-chart
convention the reference screenshot uses), with color-matched labels on the
left. Data comes exclusively from tmstore.TmSessionReader.series_after — only
fresh (measured-now) numeric samples are ever plotted, so carried repeats and
text sidecars cannot draw as measurements. Read-only like everything else on
the Live Data page; nothing here opens the serial port.

The view state caption (live / not advancing / archived / held / read failed)
is drawn on the chart itself so a frozen trace can never pass for a live one.

VCM-Scanner / EFILive cues: a time axis under the last lane (relative -m:ss
to the right edge for a live view, wall-clock HH:MM:SS for an archived
drive), a hover cursor (hairline + colour-matched readout of every trace's
value at that instant, mirrored into the graphed-channels table) and the
session's events as faint markers. Tick positions are cached per window and
traces are window_points-decimated, so paint cost stays bounded.
"""
from __future__ import annotations

import math
import sqlite3
from bisect import bisect_left, bisect_right
from datetime import datetime
from typing import Optional, Sequence

from PySide6.QtCore import Qt, QMimeData, QPointF, QRectF, QSize, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import theme, tmstore
from .tuningui import CHIP_KIND_ROLE, ChipDelegate

# Drag payload for a Live Data field: a tile drags its channel name under
# this type; the graphed-channels table is the drop target.
CHANNEL_MIME = "application/x-openobd-channel"


def channel_mime(name: str) -> QMimeData:
    m = QMimeData()
    m.setData(CHANNEL_MIME, name.encode("utf-8"))
    m.setText(name)
    return m


def mime_channel(mime) -> Optional[str]:
    if mime is None or not mime.hasFormat(CHANNEL_MIME):
        return None
    try:
        return bytes(mime.data(CHANNEL_MIME)).decode("utf-8")
    except UnicodeDecodeError:
        return None

# Preferred lane groupings, by truck-mcp store channel name. Only channels
# actually present in the bound session get a slot; store channels outside the
# presets are appended in extra lanes of up to 4 so nothing numeric is hidden.
LANE_PRESETS: list[list[str]] = [
    ["rpm", "vehicle_speed"],
    ["maf", "intake_map", "engine_load", "throttle_pos"],
    ["stft_bank1", "ltft_bank1"],
    ["coolant_temp", "intake_air_temp", "trans_fluid_temp"],
    ["control_voltage"],
]
_PRESET_NAMES = {n for lane in LANE_PRESETS for n in lane}
_EXTRA_LANE_WIDTH = 4

# Default vertical ranges in the STORE's units (the chart displays raw store
# values with their declared units — it is a provenance view, not the gauge
# view, so no unit conversion happens here). Channels without an entry
# auto-range from observed data (expanding only, never shrinking mid-view).
DEFAULT_RANGES: dict[str, tuple[float, float]] = {
    "rpm":              (0.0, 6000.0),
    "vehicle_speed":    (0.0, 200.0),     # km/h as stored
    "maf":              (0.0, 120.0),     # g/s
    "intake_map":       (0.0, 105.0),     # kPa
    "engine_load":      (0.0, 100.0),
    "throttle_pos":     (0.0, 100.0),
    "stft_bank1":       (-25.0, 25.0),
    "ltft_bank1":       (-25.0, 25.0),
    "coolant_temp":     (-20.0, 130.0),   # °C as stored
    "intake_air_temp":  (-20.0, 80.0),
    "trans_fluid_temp": (-20.0, 150.0),
    "control_voltage":  (8.0, 16.0),
}

# Per-slot channel colors within a lane (VCM-style: red, green, white, cyan…).
CHANNEL_COLORS = theme.STRIP_CHANNEL_COLORS

# View-state caption colors — same meanings as livedata.STATE_COLORS accents.
CAPTION_COLORS = theme.STRIP_CAPTION_COLORS

WINDOW_SPANS = [("30 s", 30.0), ("1 min", 60.0), ("2 min", 120.0),
                ("5 min", 300.0), ("All", None)]

# Initial-load ceiling. A session with more fresh samples than this backfills
# only its trailing tail (and says so) instead of stalling the GUI thread.
MAX_INITIAL_SAMPLES = 250_000
TRUNCATED_TAIL_S = 600.0


def chartable_names(reader: "tmstore.TmSessionReader",
                    channel_meta: dict[str, dict]) -> list[str]:
    """The channels the chart MAY plot: numeric-kind channels that are lane
    presets or already have a numeric latest sample. Which of these actually
    chart, and how they group, is the layout model's decision (chanlayout) —
    this is only the eligibility rule. A latest() read failure yields the
    preset-only set rather than raising; the page's tick surfaces read
    failures explicitly."""
    try:
        latest = reader.latest(list(channel_meta))
    except Exception:
        latest = {}
    names = []
    for name, meta in channel_meta.items():
        if (meta.get("kind") or "numeric") != "numeric":
            continue
        sample = latest.get(name) or {}
        if name in _PRESET_NAMES or sample.get("value_num") is not None:
            names.append(name)
    return names


def build_lanes(names: Sequence[str]) -> list[list[str]]:
    """Group the session's chartable channels into display lanes: the preset
    groupings first (only channels actually present), then any remaining
    channels in extra lanes of up to 4. Pure — unit-testable without Qt."""
    present = set(names)
    lanes = []
    for preset in LANE_PRESETS:
        lane = [n for n in preset if n in present]
        if lane:
            lanes.append(lane)
    rest = sorted(present - _PRESET_NAMES)
    for i in range(0, len(rest), _EXTRA_LANE_WIDTH):
        lanes.append(rest[i:i + _EXTRA_LANE_WIDTH])
    return lanes


def expand_range(lo: float, hi: float, vmin: float, vmax: float
                 ) -> tuple[float, float]:
    """Grow (lo, hi) to cover (vmin, vmax) with nice padded bounds. Never
    shrinks, so an auto-ranged trace stays stable as it scrolls."""
    if vmin < lo:
        lo = _nice_floor(vmin)
    if vmax > hi:
        hi = _nice_ceil(vmax)
    return lo, hi


def _nice_step(span: float) -> float:
    if span <= 0:
        return 1.0
    return 10.0 ** math.floor(math.log10(span))


def _nice_floor(v: float) -> float:
    step = _nice_step(abs(v) or 1.0) / 2.0
    return math.floor(v / step) * step


def _nice_ceil(v: float) -> float:
    step = _nice_step(abs(v) or 1.0) / 2.0
    return math.ceil(v / step) * step


def window_points(ts: Sequence[float], vs: Sequence[float],
                  t0: float, t1: float, max_pts: int
                  ) -> tuple[list[float], list[float]]:
    """The (t, v) points inside [t0, t1], min/max-envelope decimated to at most
    ~max_pts so painting a long archived drive stays cheap without hiding
    spikes. Pure — unit-testable without Qt."""
    i = bisect_left(ts, t0)
    j = bisect_right(ts, t1)
    # keep one point either side so the trace enters/exits the frame edges
    i = max(0, i - 1)
    j = min(len(ts), j + 1)
    n = j - i
    if n <= max_pts:
        return list(ts[i:j]), list(vs[i:j])
    buckets = max(1, max_pts // 2)
    out_t: list[float] = []
    out_v: list[float] = []
    for b in range(buckets):
        b0 = i + (n * b) // buckets
        b1 = i + (n * (b + 1)) // buckets
        if b1 <= b0:
            continue
        lo_k = hi_k = b0
        for k in range(b0 + 1, b1):
            if vs[k] < vs[lo_k]:
                lo_k = k
            if vs[k] > vs[hi_k]:
                hi_k = k
        for k in sorted({lo_k, hi_k}):
            out_t.append(ts[k])
            out_v.append(vs[k])
    return out_t, out_v


# -- time axis / cursor / window statistics (pure, unit-testable) ---------- #
#: Candidate tick intervals in seconds: "nice" clock steps.
TICK_STEPS_S = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
                7200, 10800, 21600, 43200, 86400)
TIME_MODE_RELATIVE = "relative"   # live: -m:ss back from the right edge
TIME_MODE_CLOCK = "clock"         # archived: wall-clock HH:MM:SS


def tick_step(span_s: float, max_ticks: int) -> float:
    """Smallest nice interval that puts at most max_ticks ticks on span_s."""
    if span_s <= 0 or max_ticks < 1:
        return float(TICK_STEPS_S[0])
    for step in TICK_STEPS_S:
        if span_s / step <= max_ticks:
            return float(step)
    return float(math.ceil(span_s / max_ticks / TICK_STEPS_S[-1])
                 * TICK_STEPS_S[-1])


def fmt_relative(seconds_back: float) -> str:
    """'0:00' at the right edge, '-1:30' ninety seconds back, '-1:02:03'."""
    s = int(round(abs(seconds_back)))
    if s == 0:
        return "0:00"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    body = f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
    return "-" + body


#: What a time that is not a real wall-clock instant renders as.
CLOCK_UNKNOWN = "—"


def fmt_clock(t: float, tz=None) -> str:
    """Wall-clock HH:MM:SS of an epoch second (local time unless tz). Never
    raises: it runs inside paintEvent, and a negative, non-finite or out-of-
    range time (Windows' fromtimestamp rejects negatives) renders as
    CLOCK_UNKNOWN instead of aborting the paint."""
    try:
        t = float(t)
        if not math.isfinite(t) or t < 0:
            return CLOCK_UNKNOWN
        return datetime.fromtimestamp(t, tz).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError, TypeError):
        return CLOCK_UNKNOWN


def time_ticks(t0: float, t1: float, max_ticks: int,
               mode: str = TIME_MODE_RELATIVE, tz=None
               ) -> list[tuple[float, str]]:
    """(time, label) ticks across [t0, t1], ascending. Relative mode anchors
    ticks on the right edge (0:00, -0:10, ...: a live view reads as "how long
    ago"); clock mode puts them on whole multiples of the step in wall-clock
    time (an archived drive reads as when it happened)."""
    if not (t1 > t0) or max_ticks < 1:
        return []
    if mode != TIME_MODE_RELATIVE and not (
            math.isfinite(t0) and math.isfinite(t1) and t0 >= 0):
        return []   # not a wall-clock window (e.g. the no-data placeholder)
    step = tick_step(t1 - t0, max_ticks)
    if mode == TIME_MODE_RELATIVE:
        n = int((t1 - t0) // step + 1e-9)
        return [(t1 - k * step, fmt_relative(k * step))
                for k in range(n, -1, -1)]
    k0 = math.ceil(t0 / step - 1e-9)
    k1 = math.floor(t1 / step + 1e-9)
    return [(k * step, fmt_clock(k * step, tz)) for k in range(k0, k1 + 1)]


def x_to_time(x: float, x0: float, x1: float, t0: float, t1: float) -> float:
    """Widget x inside the plot [x0, x1] -> time in [t0, t1] (clamped)."""
    if x1 <= x0:
        return t1
    frac = min(1.0, max(0.0, (x - x0) / (x1 - x0)))
    return t0 + frac * (t1 - t0)


def value_at(ts: Sequence[float], vs: Sequence[float], t: float
             ) -> Optional[float]:
    """The trace's value at time t: the last measured sample at or before t
    (sample-and-hold, as a scan tool's cursor reads). None before the first
    sample: the cursor never invents a value the store did not measure."""
    if not ts or t < ts[0]:
        return None
    return vs[bisect_right(ts, t) - 1]


def window_stats(ts: Sequence[float], vs: Sequence[float],
                 t0: float, t1: float
                 ) -> Optional[tuple[float, float, float]]:
    """(min, max, mean) of the samples with t0 <= t <= t1, or None when the
    window holds none. Exact (no decimation)."""
    i = bisect_left(ts, t0)
    j = bisect_right(ts, t1)
    if j <= i:
        return None
    window = vs[i:j]
    return min(window), max(window), sum(window) / len(window)


def fmt_value(v: Optional[float]) -> str:
    """Compact numeric text for the cursor readout and Min / Max / Avg."""
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:.0f}"
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _fmt_scale(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if v == int(v):
        return str(int(v))
    return f"{v:.1f}"


def _ts_epoch(ts_utc: Optional[str]) -> Optional[float]:
    dt = tmstore._parse_ts(ts_utc)
    return dt.timestamp() if dt else None


class _Trace:
    """One channel's data + display range. Append-only, time-ascending."""

    def __init__(self, name: str, unit: str, color: QColor):
        self.name = name
        self.unit = unit
        self.color = color
        self.ts: list[float] = []
        self.vs: list[float] = []
        rng = DEFAULT_RANGES.get(name)
        self.lo, self.hi = rng if rng else (0.0, 1.0)
        self._auto = rng is None
        self._seeded = False
        self._stats_key = None
        self._stats = None
        self._pts_key = None
        self._pts: tuple[list[float], list[float]] = ([], [])

    def points(self, t0: float, t1: float, max_pts: int
               ) -> tuple[list[float], list[float]]:
        """window_points over [t0, t1], cached until the window, the plot
        width or the data changes: a hover repaint (mouse-move rate) redraws
        the cached points instead of re-decimating a whole archived drive."""
        key = (t0, t1, int(max_pts), len(self.ts))
        if key != self._pts_key:
            self._pts_key = key
            self._pts = window_points(self.ts, self.vs, t0, t1, max_pts)
        return self._pts

    def stats(self, t0: float, t1: float
              ) -> Optional[tuple[float, float, float]]:
        """window_stats over [t0, t1], cached until the window or the data
        changes (an archived drive's table costs one scan, not one a tick)."""
        key = (t0, t1, len(self.ts))
        if key != self._stats_key:
            self._stats_key = key
            self._stats = window_stats(self.ts, self.vs, t0, t1)
        return self._stats

    def append(self, t: float, v: float):
        # series_after is ordered by insert id; a clock-skewed timestamp must
        # not make bisect windows lie, so drop out-of-order stragglers.
        if self.ts and t < self.ts[-1]:
            return
        self.ts.append(t)
        self.vs.append(v)
        if self._auto:
            if not self._seeded:
                self.lo, self.hi = _nice_floor(v), _nice_ceil(v)
                if self.lo == self.hi:
                    self.hi = self.lo + 1.0
                self._seeded = True
            else:
                self.lo, self.hi = expand_range(self.lo, self.hi, v, v)
        else:
            self.lo, self.hi = expand_range(self.lo, self.hi, v, v)

    def label(self) -> str:
        return f"{self.name} ({self.unit})" if self.unit else self.name


class StripChart(QWidget):
    """The painted chart: stacked lanes, per-channel scales right, labels
    left, a time axis under the last lane, session-event markers and a hover
    cursor with a colour-matched readout."""

    LABEL_W = 180
    SCALE_COL_W = 44
    AXIS_H = 18
    #: Minimum horizontal room per time-axis label.
    TICK_MIN_PX = 84

    #: None when the cursor leaves the plot, else (t, {name: value | None}).
    hover_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.lanes: list[list[str]] = []
        self.traces: dict[str, _Trace] = {}
        self.span: Optional[float] = 60.0     # seconds; None = whole session
        self.t_end: Optional[float] = None
        self.caption = ""
        self.empty_text = "No chartable channels in this session."
        self.time_mode = TIME_MODE_RELATIVE
        self.events: list[tuple[float, str]] = []
        self._event_ts: list[float] = []
        self._hover_x: Optional[float] = None
        self._tick_key = None
        self._ticks: list[tuple[float, str]] = []
        # Fonts built once, not per lane / per slot on every paint.
        self._lane_font = QFont()
        self._lane_font.setPointSizeF(8.0)
        self._lane_font.setBold(True)
        self._scale_font = QFont()
        self._scale_font.setPointSizeF(7.0)
        self._axis_font = QFont()
        self._axis_font.setPointSizeF(7.5)
        self._caption_font = QFont()
        self._caption_font.setPointSizeF(9.0)
        self._caption_font.setBold(True)
        self._readout_font = QFont()
        self._readout_font.setPointSizeF(8.0)
        self.setMouseTracking(True)
        self.setMinimumHeight(240)
        # Labels + a usable plot + one scale column: narrower paints garbage.
        self.setMinimumWidth(self.LABEL_W + 100 + 8 + self.SCALE_COL_W)

    def configure(self, lanes: list[list[str]], units: dict[str, str]):
        self.lanes = lanes
        self.traces = {}
        for lane in lanes:
            for slot, name in enumerate(lane):
                self.traces[name] = _Trace(
                    name, units.get(name, ""),
                    theme.hex_qcolor(
                        CHANNEL_COLORS[slot % len(CHANNEL_COLORS)]))
        self.t_end = None
        self._tick_key = None
        self.setMinimumHeight(max(210, 68 * len(lanes)) + self.AXIS_H)
        self.update()

    def append_samples(self, per_channel: dict[str, list[dict]]):
        added = False
        for name, samples in per_channel.items():
            tr = self.traces.get(name)
            if tr is None:
                continue
            for s in samples:
                t = _ts_epoch(s.get("ts_utc"))
                v = s.get("value_num")
                if t is None or v is None or not math.isfinite(float(v)):
                    continue
                tr.append(t, float(v))
                added = True
                if self.t_end is None or t > self.t_end:
                    self.t_end = t
        if added and self._hover_x is not None:
            # The window scrolled under a still mouse: the cursor now marks
            # a different instant, so the readout must follow.
            self.hover_changed.emit(self.hover_readout(self._hover_x))
        self.update()

    def set_span(self, span: Optional[float]):
        self.span = span
        self.update()

    def set_caption(self, caption: str):
        if caption != self.caption:
            self.caption = caption
            self.update()

    def set_time_mode(self, mode: str):
        if mode != self.time_mode:
            self.time_mode = mode
            self._tick_key = None
            self.update()

    def set_events(self, events: list[tuple[float, str]]):
        """Session events as (epoch seconds, short label); drawn as faint
        markers when the window includes them."""
        evs = sorted(events)
        if evs != self.events:
            self.events = evs
            self._event_ts = [t for t, _l in evs]
            self.update()

    # -- geometry ------------------------------------------------------------- #
    def lanes_height(self) -> float:
        return max(1.0, float(self.height() - self.AXIS_H))

    def lane_at(self, y: float) -> Optional[int]:
        """Index of the lane under widget-y, for the grouping context menu."""
        if not self.lanes or self.height() <= 0:
            return None
        li = int(y * len(self.lanes) / self.lanes_height())
        return li if 0 <= li < len(self.lanes) else None

    def plot_bounds(self) -> tuple[float, float]:
        """(x0, x1) of the plot area between the labels and the scales."""
        max_ch = max((len(lane) for lane in self.lanes), default=1)
        scale_w = 8 + self.SCALE_COL_W * max_ch
        x0 = float(self.LABEL_W)
        x1 = float(self.width() - scale_w)
        if x1 - x0 < 60:
            x1 = max(x0 + 60, x1)
        return x0, x1

    def _window(self) -> tuple[float, float]:
        t1 = self.t_end if self.t_end is not None else 0.0
        if self.span is None:
            starts = [tr.ts[0] for tr in self.traces.values() if tr.ts]
            t0 = min(starts) if starts else t1 - 60.0
        else:
            t0 = t1 - self.span
        if t1 <= t0:
            t1 = t0 + 1.0
        return t0, t1

    def axis_ticks(self, t0: float, t1: float, plot_w: float
                   ) -> list[tuple[float, str]]:
        """time_ticks for the current window, cached per (window, width,
        mode) so a repaint without new data does no tick arithmetic."""
        key = (t0, t1, int(plot_w), self.time_mode)
        if key != self._tick_key:
            self._tick_key = key
            self._ticks = time_ticks(
                t0, t1, max(1, int(plot_w // self.TICK_MIN_PX)),
                self.time_mode)
        return self._ticks

    def cursor_time_text(self, t: float) -> str:
        if self.time_mode == TIME_MODE_CLOCK:
            return fmt_clock(t)
        _t0, t1 = self._window()
        return fmt_relative(t1 - t)

    # -- hover cursor --------------------------------------------------------- #
    def hover_readout(self, x: Optional[float]
                      ) -> Optional[tuple[float, dict[str, Optional[float]]]]:
        """(time, {trace: value at that time}) under widget-x, or None when x
        is outside the plot or nothing is plotted."""
        if x is None or not self.lanes or not any(
                tr.ts for tr in self.traces.values()):
            return None
        x0, x1 = self.plot_bounds()
        if x < x0 or x > x1:
            return None
        t0, t1 = self._window()
        t = x_to_time(x, x0, x1, t0, t1)
        return t, {name: value_at(tr.ts, tr.vs, t)
                   for name, tr in self.traces.items()}

    def set_hover(self, x: Optional[float]):
        """Move the cursor to widget-x (None clears it) and publish the
        readout; the mouse handlers and tests both drive this."""
        readout = self.hover_readout(x)
        self._hover_x = x if readout is not None else None
        self.hover_changed.emit(readout)
        self.update()

    def mouseMoveEvent(self, event):   # noqa: N802
        self.set_hover(event.position().x())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):   # noqa: N802
        self.set_hover(None)
        super().leaveEvent(event)

    # -- paint ---------------------------------------------------------------- #
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, theme.strip_qcolor("canvas"))
        if not self.lanes:
            p.setPen(theme.strip_qcolor("empty-text"))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, self.empty_text)
            p.end()
            return

        plot_x0, plot_x1 = self.plot_bounds()
        plot_w = plot_x1 - plot_x0
        t0, t1 = self._window()
        lanes_h = self.lanes_height()
        lane_h = lanes_h / len(self.lanes)
        have_data = any(tr.ts for tr in self.traces.values())
        # No data means no real window (_window falls back to a placeholder
        # around epoch 0): draw no time axis rather than invent one.
        ticks = self.axis_ticks(t0, t1, plot_w) if have_data else []
        tick_xs = [plot_x0 + plot_w * (t - t0) / (t1 - t0) for t, _l in ticks]
        time_grid_pen = QPen(theme.strip_qcolor("time-grid"), 1)
        grid_pen = QPen(theme.strip_qcolor("gridline"), 1)

        for li, lane in enumerate(self.lanes):
            y0 = li * lane_h
            plot = QRectF(plot_x0, y0 + 1, plot_w, lane_h - 2)

            # lane backgrounds: label panel glow + plot area
            gl = QLinearGradient(0, y0, plot_x0 + 120, y0)
            gl.setColorAt(0.0, theme.strip_qcolor("label-glow-start"))
            gl.setColorAt(1.0, theme.strip_qcolor("label-glow-end"))
            p.fillRect(QRectF(0, y0, plot_x0, lane_h), gl)
            gp = QLinearGradient(plot_x0, y0, plot_x1, y0)
            gp.setColorAt(0.0, theme.strip_qcolor("plot-glow-start"))
            gp.setColorAt(0.35, theme.strip_qcolor("plot-glow-mid"))
            gp.setColorAt(1.0, theme.strip_qcolor("plot-glow-end"))
            p.fillRect(plot, gp)

            # gridlines: value quarters, then the time ticks
            p.setPen(grid_pen)
            for gi in range(1, 4):
                gy = y0 + lane_h * gi / 4.0
                p.drawLine(QPointF(plot_x0, gy), QPointF(plot_x1, gy))
            p.setPen(time_grid_pen)
            for tx in tick_xs:
                p.drawLine(QPointF(tx, y0 + 1), QPointF(tx, y0 + lane_h - 1))

            # labels (left, wrapping) + scale columns (right) + traces
            p.setFont(self._lane_font)
            fm = p.fontMetrics()
            lx, ly = theme.SPACING["md"], y0 + 6 + fm.ascent()
            for slot, name in enumerate(lane):
                tr = self.traces[name]
                text = tr.label()
                tw = fm.horizontalAdvance(text) + 14
                if lx + tw > plot_x0 and lx > theme.SPACING["md"]:
                    lx = theme.SPACING["md"]
                    ly += fm.height() + 2
                p.setPen(tr.color)
                p.drawText(QPointF(lx, ly), text)
                lx += tw

                sx = plot_x1 + 8 + slot * self.SCALE_COL_W
                p.setFont(self._scale_font)
                mid = (tr.lo + tr.hi) / 2.0
                for frac, val in ((0.0, tr.hi), (0.5, mid), (1.0, tr.lo)):
                    sy = y0 + 6 + (lane_h - 18) * frac
                    p.drawText(QRectF(sx, sy, self.SCALE_COL_W - 4, 12),
                               Qt.AlignLeft | Qt.AlignVCenter, _fmt_scale(val))
                p.setFont(self._lane_font)

                pts_t, pts_v = tr.points(t0, t1, int(plot.width()) * 2)
                if len(pts_t) >= 2:
                    span_v = (tr.hi - tr.lo) or 1.0
                    poly = []
                    for t, v in zip(pts_t, pts_v):
                        x = plot.x() + plot.width() * (t - t0) / (t1 - t0)
                        y = plot.bottom() - 3 - (plot.height() - 6) * \
                            (min(max(v, tr.lo), tr.hi) - tr.lo) / span_v
                        poly.append(QPointF(x, y))
                    p.setClipRect(plot)
                    p.setPen(QPen(tr.color, 1.2))
                    for a, b in zip(poly, poly[1:]):
                        p.drawLine(a, b)
                    p.setClipping(False)

            # lane separator
            p.setPen(QPen(theme.strip_qcolor("lane-separator"), 1))
            p.drawLine(QPointF(0, y0 + lane_h), QPointF(w, y0 + lane_h))

        self._paint_events(p, t0, t1, plot_x0, plot_w, lanes_h)
        self._paint_axis(p, ticks, tick_xs, plot_x0, plot_x1, lanes_h, w)

        # view-state caption — a frozen trace must announce what it is
        if self.caption:
            p.setFont(self._caption_font)
            p.setPen(theme.hex_qcolor(CAPTION_COLORS.get(
                self.caption, theme.STRIP_COLORS["caption-fallback"])))
            p.drawText(QRectF(plot_x0, 4, plot_w - 8, 16),
                       Qt.AlignRight | Qt.AlignVCenter, self.caption)
        if not have_data:
            p.setPen(theme.strip_qcolor("empty-text"))
            p.drawText(QRectF(plot_x0, 0, plot_w, lanes_h), Qt.AlignCenter,
                       "No fresh numeric samples yet.")
        elif self._hover_x is not None:
            self._paint_cursor(p, plot_x0, plot_x1, lanes_h)
        p.end()

    def _paint_events(self, p: QPainter, t0: float, t1: float,
                      plot_x0: float, plot_w: float, lanes_h: float):
        i = bisect_left(self._event_ts, t0)
        j = bisect_right(self._event_ts, t1)
        if j <= i:
            return
        pen = QPen(theme.strip_qcolor("event-marker", 110), 1, Qt.DashLine)
        p.setFont(self._axis_font)
        for t, label in self.events[i:j]:
            x = plot_x0 + plot_w * (t - t0) / (t1 - t0)
            p.setPen(pen)
            p.drawLine(QPointF(x, 0), QPointF(x, lanes_h))
            p.setPen(theme.strip_qcolor("event-marker", 200))
            p.drawText(QRectF(x + 3, 4, 110, 12),
                       Qt.AlignLeft | Qt.AlignVCenter, label)

    def _paint_axis(self, p: QPainter, ticks, tick_xs, plot_x0: float,
                    plot_x1: float, lanes_h: float, w: float):
        p.fillRect(QRectF(0, lanes_h, w, self.AXIS_H),
                   theme.strip_qcolor("canvas"))
        p.setFont(self._axis_font)
        p.setPen(theme.strip_qcolor("axis-text"))
        p.drawText(QRectF(theme.SPACING["md"], lanes_h, plot_x0, self.AXIS_H),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   "time (HH:MM:SS)" if self.time_mode == TIME_MODE_CLOCK
                   else "time (m:ss ago)")
        tick_pen = QPen(theme.strip_qcolor("axis-tick"), 1)
        for (_t, label), x in zip(ticks, tick_xs):
            p.setPen(tick_pen)
            p.drawLine(QPointF(x, lanes_h), QPointF(x, lanes_h + 4))
            # Keep edge labels inside the plot band.
            lw = self.TICK_MIN_PX - 8
            lx = min(max(x - lw / 2, plot_x0), plot_x1 - lw)
            p.setPen(theme.strip_qcolor("axis-text"))
            p.drawText(QRectF(lx, lanes_h + 3, lw, self.AXIS_H - 3),
                       Qt.AlignHCenter | Qt.AlignVCenter, label)

    def _paint_cursor(self, p: QPainter, plot_x0: float, plot_x1: float,
                      lanes_h: float):
        readout = self.hover_readout(self._hover_x)
        if readout is None:
            return
        t, values = readout
        x = self._hover_x
        p.setPen(QPen(theme.strip_qcolor("cursor", 170), 1))
        p.drawLine(QPointF(x, 0), QPointF(x, lanes_h))
        p.setFont(self._readout_font)
        fm = p.fontMetrics()
        rows = [(self.cursor_time_text(t), theme.strip_qcolor("cursor"))]
        for lane in self.lanes:
            for name in lane:
                tr = self.traces[name]
                unit = f" {tr.unit}" if tr.unit else ""
                rows.append((f"{name}  {fmt_value(values.get(name))}{unit}",
                             tr.color))
        lh = fm.height()
        box_w = max(fm.horizontalAdvance(text) for text, _c in rows) + 12
        box_h = lh * len(rows) + 8
        bx = x + 10 if x + 10 + box_w <= plot_x1 else x - 10 - box_w
        bx = max(plot_x0, bx)
        by = 22.0
        p.setPen(QPen(theme.strip_qcolor("cursor-box-border"), 1))
        p.setBrush(theme.strip_qcolor("cursor-box-bg", 230))
        p.drawRoundedRect(QRectF(bx, by, box_w, box_h),
                          theme.RADIUS["sm"], theme.RADIUS["sm"])
        p.setBrush(Qt.NoBrush)
        for k, (text, color) in enumerate(rows):
            p.setPen(color)
            p.drawText(QPointF(bx + 6, by + 4 + fm.ascent() + k * lh), text)


class ChannelTable(QTableWidget):
    """The graphed-channels row table above the strip chart (VCM-Scanner
    style): one row per charted channel in lane order, with a colour swatch
    matching its trace, its live value rendered through the same
    display_state discipline as the tiles, and Min / Max / Avg over the
    chart's visible window. While the mouse is over the chart, the Value
    column shows the cursor's values instead (header "@ Cursor"); leaving
    the chart restores the live values.

    The DROP TARGET for tile drags: dropping on a row joins that row's lane;
    dropping below the rows (or on the empty-state row) starts a new lane.
    What a drop MEANS is the page's shared layout model's decision — this
    table only reports (name, lane) via channel_dropped."""

    channel_dropped = Signal(str, object)   # channel name, lane index or None

    COLUMNS = ("Field", "Value", "Unit", "Min", "Max", "Avg", "Lane")
    (COL_FIELD, COL_VALUE, COL_UNIT, COL_MIN, COL_MAX, COL_AVG,
     COL_LANE) = range(7)
    HEADER_TIPS = (
        "Graphed field; the swatch matches its trace colour",
        "Latest value, with the same live / not advancing / archived / "
        "carried rules as the tiles (the cursor's value while hovering "
        "the chart)",
        "Unit as stored by truck-mcp",
        "Lowest measured value in the visible chart window",
        "Highest measured value in the visible chart window",
        "Mean of the measured values in the visible chart window",
        "Chart lane this field draws in",
    )
    EMPTY_TEXT = "Drag a tile here to graph it"

    def __init__(self):
        super().__init__(0, len(self.COLUMNS))
        self.setObjectName("graphTable")
        self.setHorizontalHeaderLabels(list(self.COLUMNS))
        for c, tip in enumerate(self.HEADER_TIPS):
            self.horizontalHeaderItem(c).setToolTip(tip)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(theme.ROW_HEIGHT)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setShowGrid(False)
        self.setAcceptDrops(True)
        self.setIconSize(QSize(theme.ICON_SIZE - 4, theme.ICON_SIZE - 4))
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(self.COL_FIELD, QHeaderView.Stretch)
        for c in range(1, len(self.COLUMNS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.setItemDelegateForColumn(self.COL_LANE, ChipDelegate(self))
        self._names: list[str] = []
        self._lane_of: dict[str, int] = {}
        self._droppable: set[str] = set()
        self._live_texts: dict[str, str] = {}
        self._cursor_on = False
        self._held = False
        self._incomplete: set[str] = set()
        self._show_empty_row()

    def set_droppable(self, names):
        """The channels a drop may add — the chartable set. A text/no-data
        field is refused visibly (forbidden cursor), never accepted-and-lost."""
        self._droppable = set(names)

    def channel_at(self, row: int) -> Optional[str]:
        return self._names[row] if 0 <= row < len(self._names) else None

    def is_empty_state(self) -> bool:
        return not self._names

    #: Tallest the table grows before it scrolls.
    MAX_HEIGHT = 180

    def _fit_height(self, rows: int):
        """Size to the rows (no dead band under a two-row table), up to
        MAX_HEIGHT, so the chart keeps the room."""
        h = (self.horizontalHeader().sizeHint().height()
             + rows * theme.ROW_HEIGHT + 2 * self.frameWidth() + 2)
        self.setFixedHeight(min(self.MAX_HEIGHT, h))

    def _show_empty_row(self):
        self._fit_height(1)
        self.clearSpans()
        self.setRowCount(1)
        self.setSpan(0, 0, 1, len(self.COLUMNS))
        it = QTableWidgetItem(self.EMPTY_TEXT)
        it.setFlags(Qt.ItemIsEnabled)
        it.setTextAlignment(Qt.AlignCenter)
        it.setForeground(theme.qcolor("text-muted"))
        self.setItem(0, 0, it)

    def set_rows(self, rows: list[tuple[str, str, int, QColor]]):
        """(name, unit, lane_index, trace color) per graphed channel."""
        self._names = [r[0] for r in rows]
        self._lane_of = {r[0]: r[2] for r in rows}
        self._live_texts = {}
        self._cursor_on = False
        self._incomplete = set()
        self.horizontalHeaderItem(self.COL_VALUE).setText(self._value_header())
        if not rows:
            self._show_empty_row()
            return
        self.clearSpans()
        self.setRowCount(len(rows))
        self._fit_height(len(rows))
        muted = theme.qcolor("text-muted")
        for i, (name, unit, lane_i, color) in enumerate(rows):
            cell = QTableWidgetItem(theme.swatch_icon(color), name)
            cell.setForeground(color)
            self.setItem(i, self.COL_FIELD, cell)
            val = QTableWidgetItem("—")
            val.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.setItem(i, self.COL_VALUE, val)
            self._style_value(val)
            unit_it = QTableWidgetItem(unit)
            unit_it.setForeground(muted)
            self.setItem(i, self.COL_UNIT, unit_it)
            for c in (self.COL_MIN, self.COL_MAX, self.COL_AVG):
                st = QTableWidgetItem("—")
                st.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                st.setForeground(muted)
                self.setItem(i, c, st)
            lane_it = QTableWidgetItem(f"Lane {lane_i + 1}")
            lane_it.setData(CHIP_KIND_ROLE, "neutral")
            self.setItem(i, self.COL_LANE, lane_it)

    #: Value column header while the page is held.
    HELD_HEADER = "Value · held"

    def _value_header(self) -> str:
        return self.HELD_HEADER if self._held else "Value"

    def _style_value(self, item: QTableWidgetItem):
        """A held value takes the held caption colour; otherwise the
        default foreground (a frozen value must never read as live)."""
        if self._held:
            item.setForeground(theme.hex_qcolor(CAPTION_COLORS["held"]))
        else:
            item.setData(Qt.ForegroundRole, None)

    def set_held(self, on: bool):
        """Mark (or unmark) the Value column as held: header text and the
        held colour on every value cell."""
        on = bool(on)
        if on == self._held:
            return
        self._held = on
        if not self._cursor_on:
            self.horizontalHeaderItem(self.COL_VALUE).setText(
                self._value_header())
        for i in range(len(self._names)):
            it = self.item(i, self.COL_VALUE)
            if it is not None:
                self._style_value(it)

    def is_held(self) -> bool:
        return self._held

    def _write_values(self, texts: dict[str, str]):
        for i, name in enumerate(self._names):
            text = texts.get(name)
            it = self.item(i, self.COL_VALUE)
            if text is not None and it is not None:
                it.setText(text)

    def update_values(self, texts: dict[str, str]):
        """Live values (display_state text). Shown unless the chart cursor
        is active, in which case they are kept for the restore."""
        self._live_texts.update(texts)
        if not self._cursor_on:
            self._write_values(texts)

    def show_cursor(self, texts: Optional[dict[str, str]]):
        """Cursor values while hovering the chart; None restores live."""
        if texts is None:
            if self._cursor_on:
                self._cursor_on = False
                self.horizontalHeaderItem(self.COL_VALUE).setText(
                    self._value_header())
                self._write_values(self._live_texts)
            return
        self._cursor_on = True
        self.horizontalHeaderItem(self.COL_VALUE).setText("@ Cursor")
        self._write_values(texts)

    #: Stats text of a graphed field whose history failed to load: the trace
    #: is incomplete, so its window stats would be silently partial.
    NOT_LOADED = "not loaded"

    def set_incomplete(self, names):
        """Mark the rows whose trace history has not loaded (a backfill read
        failed): their Min / Max / Avg read "not loaded" in the read-failed
        caption colour until set_incomplete clears them."""
        self._incomplete = {n for n in names if n in self._names}
        failed = theme.hex_qcolor(CAPTION_COLORS["read failed"])
        muted = theme.qcolor("text-muted")
        for i, name in enumerate(self._names):
            for c in (self.COL_MIN, self.COL_MAX, self.COL_AVG):
                it = self.item(i, c)
                if it is None:
                    continue
                if name in self._incomplete:
                    it.setText(self.NOT_LOADED)
                    it.setForeground(failed)
                else:
                    it.setForeground(muted)

    def incomplete(self) -> set[str]:
        return set(self._incomplete)

    def set_stats(self, stats: dict[str, Optional[tuple[float, float, float]]]):
        for i, name in enumerate(self._names):
            if name in self._incomplete:
                continue
            st = stats.get(name)
            for c, k in ((self.COL_MIN, 0), (self.COL_MAX, 1),
                         (self.COL_AVG, 2)):
                it = self.item(i, c)
                if it is not None:
                    it.setText(fmt_value(st[k]) if st else "—")

    def text(self, name: str, column: int) -> Optional[str]:
        """Cell text of a graphed field's row (tests / accessibility)."""
        if name not in self._names:
            return None
        it = self.item(self._names.index(name), column)
        return it.text() if it is not None else None

    # -- drops --------------------------------------------------------------- #
    def _acceptable(self, event) -> bool:
        name = mime_channel(event.mimeData())
        return name is not None and name in self._droppable

    def dragEnterEvent(self, event):   # noqa: N802
        if self._acceptable(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):   # noqa: N802
        if self._acceptable(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):   # noqa: N802
        if not self._acceptable(event):
            event.ignore()
            return
        name = mime_channel(event.mimeData())
        pos = self.viewport().mapFrom(self, event.position().toPoint())
        row = self.indexAt(pos).row()
        lane = self._lane_of.get(self.channel_at(row)) if row >= 0 else None
        event.acceptProposedAction()
        self.channel_dropped.emit(name, lane)


#: Session events fetched per read (newest first in the store).
EVENT_LIMIT = 200


class ChartPane(QWidget):
    """Chart vs. Time: graphed-channels table + strip chart.

    Owns an incremental cursor over the bound session (sample id, via
    tmstore.series_after) so each 1 Hz tick reads only what is new. The
    reader itself is owned by LiveDataPage; this pane only queries it.

    The window-span combo is always the pane's (span_text / set_span_text);
    own_bar=False leaves it unplaced so a host page can put it in its own
    toolbar (Live Data does), keeping one toolbar per page."""

    def __init__(self, own_bar: bool = True):
        super().__init__()
        self._reader: Optional[tmstore.TmSessionReader] = None
        self._channels: list[str] = []
        self._cursor = 0
        self._min_ts: Optional[str] = None
        # Channel -> hold bound whose history (ids 1..bound) has not been read:
        # a frozen backfill failed. The next tick loads it before any newer
        # row, and the table shows the channel "not loaded" until it does.
        self._needs_backfill: dict[str, int] = {}
        # Channel -> highest id already read for it past the shared cursor (a
        # frozen backfill up to the hold): later reads skip those ids, so a
        # row is never appended twice.
        self._floor: dict[str, int] = {}
        self._events_ok = True
        self._own_bar = own_bar

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        self._span_combo = QComboBox()
        self._span_combo.setToolTip("Chart window: how much time the traces "
                                    "and the Min / Max / Avg columns cover")
        for label, _span in WINDOW_SPANS:
            self._span_combo.addItem(label)
        self._span_combo.setCurrentText("1 min")
        self._span_combo.currentIndexChanged.connect(self._on_span)
        self._note = QLabel("")
        self._note.setTextFormat(Qt.PlainText)   # carries exception text
        self._note.setObjectName("chartNote")
        self._note.setStyleSheet(
            f"color:{theme.STRIP_COLORS['secondary-text']}; font-size:11px;")
        if own_bar:
            bar = QHBoxLayout()
            bar.addWidget(QLabel("Window:"))
            bar.addWidget(self._span_combo)
            bar.addWidget(self._note, 1)
            root.addLayout(bar)
        else:
            self._note.setWordWrap(True)
            self._note.hide()
            root.addWidget(self._note)
        # Graphed-channels table above the chart: the drop target for tile
        # drags, and the row-per-trace legend with live values and stats.
        self.table = ChannelTable()
        root.addWidget(self.table)
        self.chart = StripChart()
        self.chart.hover_changed.connect(self._on_hover)
        root.addWidget(self.chart, 1)

    @property
    def span_combo(self) -> QComboBox:
        return self._span_combo

    def _set_note(self, text: str):
        self._note.setText(text)
        if not self._own_bar:
            self._note.setVisible(bool(text))

    # -- binding -------------------------------------------------------------- #
    def bind(self, reader: tmstore.TmSessionReader,
             channel_meta: dict[str, dict], archived: bool,
             lanes: list[list[str]], frozen_upto: Optional[int] = None):
        """Configure the given lanes and backfill. The lanes come from the
        page's shared ChannelLayout (chanlayout) — the ONE model both the tile
        grid and this chart derive from — so this pane never decides for
        itself what is selected; it only renders and reads.

        frozen_upto (the page is held and only the layout changed: the
        store's highest sample id as of the page's last read before the
        hold): rebuild the lanes and table, keep every surviving trace's
        samples, the cursor, the backfill bound, the note and the window.
        A channel newly added to the graph is backfilled up to the hold
        instant (it matches the frozen tiles); the shared cursor does NOT
        move, so release reads every surviving trace's unread rows, and each
        sample lands exactly once."""
        if (frozen_upto is not None and self._reader is reader
                and reader is not None):
            self._rebind_frozen(channel_meta, lanes, int(frozen_upto))
            return
        self.unbind()
        names = [n for lane in lanes for n in lane]
        units = {n: (channel_meta.get(n) or {}).get("unit") or "" for n in names}
        self.chart.configure(lanes, units)
        self.table.set_rows([
            (n, units[n], li, self.chart.traces[n].color)
            for li, lane in enumerate(lanes) for n in lane])
        self._reader = reader
        self._channels = [n for lane in lanes for n in lane]
        self._cursor = 0
        self._events_ok = True
        note = ""
        min_ts = None
        if self._channels:
            try:
                total = reader.sample_count(self._channels, fresh_only=True)
            except Exception:
                total = 0
            if total > MAX_INITIAL_SAMPLES:
                # Refuse the unbounded load; chart only the trailing tail and
                # say so — silent truncation would read as full coverage.
                newest = _ts_epoch(reader.latest_ts())
                if newest is not None:
                    from datetime import timezone
                    min_ts = datetime.fromtimestamp(
                        newest - TRUNCATED_TAIL_S, tz=timezone.utc).isoformat()
                note = (f"large session ({total:,} samples) — charting the "
                        f"last {TRUNCATED_TAIL_S / 60:.0f} min only")
        self._set_note(note)
        self._min_ts = min_ts
        self.chart.set_time_mode(TIME_MODE_CLOCK if archived
                                 else TIME_MODE_RELATIVE)
        self._load_events()
        # Per-session default window: an archived drive opens on the whole
        # drive (the interesting part is rarely the idle tail); a live one
        # follows the last minute. The combo stays user-adjustable after bind.
        self._span_combo.setCurrentText("All" if archived else "1 min")
        self.tick(archived=archived, session_stale=False)

    def _rebind_frozen(self, channel_meta: dict[str, dict],
                       lanes: list[list[str]], upto: int):
        # Every surviving trace was read exactly up to self._cursor (a failed
        # chart read leaves the cursor where it was), so the cursor never
        # moves here: rows past it were not read for those traces and release
        # must read them. A newly graphed channel was never read at all; the
        # hold instant for it is the later of the cursor and the page's last
        # read bound (that channel's rows up to `upto` were recorded before
        # the hold and are what its tile shows).
        bound = max(self._cursor, upto)
        old = self.chart.traces
        caption = self.chart.caption
        names = [n for lane in lanes for n in lane]
        units = {n: (channel_meta.get(n) or {}).get("unit") or "" for n in names}
        self.chart.set_hover(None)
        self.chart.configure(lanes, units)
        new_names = []
        for name, tr in self.chart.traces.items():
            prev = old.get(name)
            if prev is None:
                new_names.append(name)
                continue
            tr.ts, tr.vs = prev.ts, prev.vs
            tr.lo, tr.hi = prev.lo, prev.hi
            tr._auto, tr._seeded = prev._auto, prev._seeded
        # A channel still owing its history from an earlier failed backfill
        # is retried with the newly graphed ones.
        backfill = {n: bound for n in new_names}
        backfill.update({n: b for n, b in self._needs_backfill.items()
                         if n in self.chart.traces and n not in backfill})
        self._floor = {n: f for n, f in self._floor.items()
                       if n in self.chart.traces}
        self._needs_backfill = {}
        if backfill and not self._backfill(backfill):
            self._needs_backfill = backfill
        ends = [tr.ts[-1] for tr in self.chart.traces.values() if tr.ts]
        self.chart.t_end = max(ends) if ends else None
        self.chart.set_caption(caption)
        self.table.set_rows([
            (n, units[n], li, self.chart.traces[n].color)
            for li, lane in enumerate(lanes) for n in lane])
        self._channels = names
        self.table.set_incomplete(self._needs_backfill.keys())
        self.refresh_stats()
        self.chart.update()

    def _backfill(self, bounds: dict[str, int]) -> bool:
        """Load each channel's history up to its bound (ids 1..bound) and
        record the bound as that channel's floor, so a later read past the
        shared cursor skips what was loaded here. False (and the failure
        shown) when the read raised; nothing is appended then."""
        if not any(b > 0 for b in bounds.values()):
            return True
        try:
            rows = self._reader.series_after(
                list(bounds), after_id=0, min_ts=self._min_ts)
        except Exception as exc:
            self.set_failed(str(exc))
            return False
        self.chart.append_samples({
            n: [s for s in samples
                if s.get("id") is not None and s["id"] <= bounds.get(n, 0)]
            for n, samples in rows.items()})
        for n, b in bounds.items():
            if b > self._cursor:
                self._floor[n] = b
        return True

    def unbind(self):
        self._reader = None
        self._channels = []
        self._cursor = 0
        self._min_ts = None
        self._needs_backfill = {}
        self._floor = {}
        self.chart.set_hover(None)
        self.chart.configure([], {})
        self.chart.set_caption("")
        self.chart.set_events([])
        self.table.set_rows([])
        self.table.set_droppable(())
        self._set_note("")

    def _load_events(self):
        """Session events as chart markers. Decoration only: a store without
        an event table disables them for this binding; any other read error
        keeps the last good set (the page's tick reports read failures)."""
        if self._reader is None or not self._events_ok:
            return
        try:
            rows = self._reader.events(limit=EVENT_LIMIT)
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                self._events_ok = False
            return
        except (sqlite3.Error, tmstore.TmSessionError, OSError):
            return
        evs = []
        for r in rows:
            t = _ts_epoch(r.get("ts_utc"))
            if t is None:
                continue
            label = str(r.get("label") or r.get("kind") or "event")
            evs.append((t, label[:16]))
        self.chart.set_events(evs)

    def update_table(self, latest: dict[str, dict],
                     channel_meta: dict[str, dict], *,
                     archived: bool, session_stale: bool):
        """Refresh the table's Value column through display_state — the SAME
        honesty rules as the tiles, so a carried/stale/archived value can't
        read as live in the legend either."""
        texts: dict[str, str] = {}
        for name in self._channels:
            try:
                ds = tmstore.display_state(
                    latest.get(name), channel_meta.get(name),
                    archived=archived, session_stale=session_stale)
                texts[name] = ds.text
            except Exception:
                texts[name] = "BAD"
        self.table.update_values(texts)

    def refresh_stats(self):
        """Min / Max / Avg over the chart's visible window, from the traces
        (exact, not the decimated paint points)."""
        if not self._channels:
            return
        t0, t1 = self.chart._window()
        self.table.set_stats({n: tr.stats(t0, t1)
                              for n, tr in self.chart.traces.items()})

    # -- per-tick ------------------------------------------------------------- #
    def tick(self, *, archived: bool, session_stale: bool):
        if self._reader is None or not self._channels:
            return
        if self._needs_backfill:
            # History first, so no row newer than the cursor is appended to a
            # trace still missing its older ones; until it loads nothing
            # advances and the failure stays shown.
            pending = {n: b for n, b in self._needs_backfill.items()
                       if n in self._channels}
            if pending and not self._backfill(pending):
                self.table.set_incomplete(pending.keys())
                return
            self._needs_backfill = {}
            self.table.set_incomplete(())
        try:
            fresh = self._reader.series_after(
                self._channels, after_id=self._cursor, min_ts=self._min_ts)
        except Exception as exc:
            self.set_failed(str(exc))
            return
        for samples in fresh.values():
            for s in samples:
                sid = s.get("id")
                if sid is not None and sid > self._cursor:
                    self._cursor = sid
        if self._floor:
            # Rows a frozen backfill already loaded for that channel.
            fresh = {n: [s for s in samples
                         if s.get("id") is None
                         or s["id"] > self._floor.get(n, 0)]
                     for n, samples in fresh.items()}
            self._floor = {n: f for n, f in self._floor.items()
                           if f > self._cursor}
        self.chart.append_samples(fresh)
        if archived:
            self.chart.set_time_mode(TIME_MODE_CLOCK)
        else:
            self._load_events()
        self.refresh_stats()
        self.chart.set_caption(
            "archived drive" if archived
            else "not advancing" if session_stale else "live")

    def set_failed(self, message: str):
        self.chart.set_caption("read failed")
        self._set_note(f"read failed: {message}"[:120])

    def _on_span(self, idx: int):
        self.chart.set_span(WINDOW_SPANS[idx][1])
        self.refresh_stats()

    def _on_hover(self, readout):
        if readout is None:
            self.table.show_cursor(None)
            return
        _t, values = readout
        self.table.show_cursor({n: fmt_value(v) for n, v in values.items()})

    # A layout change rebinds the pane; the page uses these to keep the user's
    # chosen window across that rebind instead of snapping back to the default.
    def span_text(self) -> str:
        return self._span_combo.currentText()

    def set_span_text(self, text: str):
        if any(label == text for label, _s in WINDOW_SPANS):
            self._span_combo.setCurrentText(text)
