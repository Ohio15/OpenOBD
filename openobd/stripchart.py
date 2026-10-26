"""
stripchart — VCM-Scanner-style "Chart vs. Time" view over a truck-mcp session.

Stacked lanes, several channels per lane; each channel keeps its own vertical
scale, drawn as a color-matched min/mid/max column on the right (the strip-chart
convention the reference screenshot uses), with color-matched labels on the
left. Data comes exclusively from tmstore.TmSessionReader.series_after — only
fresh (measured-now) numeric samples are ever plotted, so carried repeats and
text sidecars cannot draw as measurements. Read-only like everything else on
the Live Data page; nothing here opens the serial port.

The view state caption (live / not advancing / archived / read failed) is drawn
on the chart itself so a frozen trace can never pass for a live one.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from typing import Optional, Sequence

from PySide6.QtCore import Qt, QMimeData, QPointF, QRectF, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import theme, tmstore

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
    """The painted chart: stacked lanes, per-channel scales right, labels left."""

    LABEL_W = 180
    SCALE_COL_W = 44

    def __init__(self):
        super().__init__()
        self.lanes: list[list[str]] = []
        self.traces: dict[str, _Trace] = {}
        self.span: Optional[float] = 60.0     # seconds; None = whole session
        self.t_end: Optional[float] = None
        self.caption = ""
        self.empty_text = "No chartable channels in this session."
        self.setMinimumHeight(240)

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
        self.setMinimumHeight(max(210, 68 * len(lanes)))
        self.update()

    def append_samples(self, per_channel: dict[str, list[dict]]):
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
                if self.t_end is None or t > self.t_end:
                    self.t_end = t
        self.update()

    def set_span(self, span: Optional[float]):
        self.span = span
        self.update()

    def set_caption(self, caption: str):
        if caption != self.caption:
            self.caption = caption
            self.update()

    def lane_at(self, y: float) -> Optional[int]:
        """Index of the lane under widget-y, for the grouping context menu."""
        if not self.lanes or self.height() <= 0:
            return None
        li = int(y * len(self.lanes) / self.height())
        return li if 0 <= li < len(self.lanes) else None

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

        max_ch = max(len(lane) for lane in self.lanes)
        scale_w = 8 + self.SCALE_COL_W * max_ch
        plot_x0 = self.LABEL_W
        plot_x1 = w - scale_w
        if plot_x1 - plot_x0 < 60:
            plot_x1 = max(plot_x0 + 60, plot_x1)
        t0, t1 = self._window()
        lane_h = h / len(self.lanes)
        have_data = any(tr.ts for tr in self.traces.values())

        for li, lane in enumerate(self.lanes):
            y0 = li * lane_h
            plot = QRectF(plot_x0, y0 + 1, plot_x1 - plot_x0, lane_h - 2)

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

            # gridlines
            p.setPen(QPen(theme.strip_qcolor("gridline"), 1))
            for gi in range(1, 4):
                gy = y0 + lane_h * gi / 4.0
                p.drawLine(QPointF(plot_x0, gy), QPointF(plot_x1, gy))

            # labels (left, wrapping) + scale columns (right) + traces
            lf = QFont(); lf.setPointSizeF(8.0); lf.setBold(True)
            p.setFont(lf)
            fm = p.fontMetrics()
            lx, ly = 8, y0 + 6 + fm.ascent()
            for slot, name in enumerate(lane):
                tr = self.traces[name]
                text = tr.label()
                tw = fm.horizontalAdvance(text) + 14
                if lx + tw > plot_x0 and lx > 8:
                    lx = 8
                    ly += fm.height() + 2
                p.setPen(tr.color)
                p.drawText(QPointF(lx, ly), text)
                lx += tw

                sx = plot_x1 + 8 + slot * self.SCALE_COL_W
                sf = QFont(); sf.setPointSizeF(7.0); p.setFont(sf)
                p.setPen(tr.color)
                mid = (tr.lo + tr.hi) / 2.0
                for frac, val in ((0.0, tr.hi), (0.5, mid), (1.0, tr.lo)):
                    sy = y0 + 6 + (lane_h - 18) * frac
                    p.drawText(QRectF(sx, sy, self.SCALE_COL_W - 4, 12),
                               Qt.AlignLeft | Qt.AlignVCenter, _fmt_scale(val))
                p.setFont(lf)

                pts_t, pts_v = window_points(
                    tr.ts, tr.vs, t0, t1, int(plot.width()) * 2)
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

        # view-state caption — a frozen trace must announce what it is
        if self.caption:
            cf = QFont(); cf.setPointSizeF(9.0); cf.setBold(True)
            p.setFont(cf)
            p.setPen(theme.hex_qcolor(CAPTION_COLORS.get(
                self.caption, theme.STRIP_COLORS["caption-fallback"])))
            p.drawText(QRectF(plot_x0, 4, plot_x1 - plot_x0 - 8, 16),
                       Qt.AlignRight | Qt.AlignVCenter, self.caption)
        if not have_data:
            p.setPen(theme.strip_qcolor("empty-text"))
            p.drawText(QRectF(plot_x0, 0, plot_x1 - plot_x0, h), Qt.AlignCenter,
                       "No fresh numeric samples yet.")
        p.end()


class ChannelTable(QTableWidget):
    """The graphed-channels row table above the strip chart (VCM-Scanner
    style): one row per charted channel in lane order, color-matched to its
    trace, with its live value rendered through the same display_state
    discipline as the tiles. The DROP TARGET for tile drags: dropping on a
    row joins that row's lane; dropping below the rows starts a new lane.
    What a drop MEANS is the page's shared layout model's decision — this
    table only reports (name, lane) via channel_dropped."""

    channel_dropped = Signal(str, object)   # channel name, lane index or None

    def __init__(self):
        super().__init__(0, 4)
        self.setHorizontalHeaderLabels(["Field", "Value", "Unit", "Lane"])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAcceptDrops(True)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            self.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.ResizeToContents)
        self.setStyleSheet(theme.strip_readout_qss())
        self.setMaximumHeight(180)
        self._names: list[str] = []
        self._lane_of: dict[str, int] = {}
        self._droppable: set[str] = set()

    def set_droppable(self, names):
        """The channels a drop may add — the chartable set. A text/no-data
        field is refused visibly (forbidden cursor), never accepted-and-lost."""
        self._droppable = set(names)

    def channel_at(self, row: int) -> Optional[str]:
        return self._names[row] if 0 <= row < len(self._names) else None

    def set_rows(self, rows: list[tuple[str, str, int, QColor]]):
        """(name, unit, lane_index, trace color) per graphed channel."""
        self._names = [r[0] for r in rows]
        self._lane_of = {r[0]: r[2] for r in rows}
        self.setRowCount(len(rows))
        for i, (name, unit, lane_i, color) in enumerate(rows):
            cell = QTableWidgetItem("■ " + name)
            cell.setForeground(color)
            self.setItem(i, 0, cell)
            val = QTableWidgetItem("—")
            val.setForeground(theme.strip_qcolor("readout-text"))
            self.setItem(i, 1, val)
            unit_it = QTableWidgetItem(unit)
            unit_it.setForeground(theme.strip_qcolor("secondary-text"))
            self.setItem(i, 2, unit_it)
            lane_it = QTableWidgetItem(str(lane_i + 1))
            lane_it.setForeground(theme.strip_qcolor("secondary-text"))
            lane_it.setTextAlignment(Qt.AlignCenter)
            self.setItem(i, 3, lane_it)

    def update_values(self, texts: dict[str, str]):
        for i, name in enumerate(self._names):
            text = texts.get(name)
            if text is not None and self.item(i, 1) is not None:
                self.item(i, 1).setText(text)

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


class ChartPane(QWidget):
    """Chart vs. Time sub-page of Live Data: window selector + strip chart.

    Owns an incremental cursor over the bound session (sample id, via
    tmstore.series_after) so each 1 Hz tick reads only what is new. The
    reader itself is owned by LiveDataPage; this pane only queries it."""

    def __init__(self):
        super().__init__()
        self._reader: Optional[tmstore.TmSessionReader] = None
        self._channels: list[str] = []
        self._cursor = 0
        self._min_ts: Optional[str] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Window:"))
        self._span_combo = QComboBox()
        for label, _span in WINDOW_SPANS:
            self._span_combo.addItem(label)
        self._span_combo.setCurrentText("1 min")
        self._span_combo.currentIndexChanged.connect(self._on_span)
        bar.addWidget(self._span_combo)
        self._note = QLabel("")
        self._note.setStyleSheet(
            f"color:{theme.STRIP_COLORS['secondary-text']}; font-size:11px;")
        bar.addWidget(self._note, 1)
        root.addLayout(bar)
        # Graphed-channels table between the toolbar and the chart: the drop
        # target for tile drags, and the row-per-trace legend with live values.
        self.table = ChannelTable()
        root.addWidget(self.table)
        self.chart = StripChart()
        root.addWidget(self.chart, 1)

    # -- binding -------------------------------------------------------------- #
    def bind(self, reader: tmstore.TmSessionReader,
             channel_meta: dict[str, dict], archived: bool,
             lanes: list[list[str]]):
        """Configure the given lanes and backfill. The lanes come from the
        page's shared ChannelLayout (chanlayout) — the ONE model both the tile
        grid and this chart derive from — so this pane never decides for
        itself what is selected; it only renders and reads."""
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
                    from datetime import datetime, timezone
                    min_ts = datetime.fromtimestamp(
                        newest - TRUNCATED_TAIL_S, tz=timezone.utc).isoformat()
                note = (f"large session ({total:,} samples) — charting the "
                        f"last {TRUNCATED_TAIL_S / 60:.0f} min only")
        self._note.setText(note)
        self._min_ts = min_ts
        # Per-session default window: an archived drive opens on the whole
        # drive (the interesting part is rarely the idle tail); a live one
        # follows the last minute. The combo stays user-adjustable after bind.
        self._span_combo.setCurrentText("All" if archived else "1 min")
        self.tick(archived=archived, session_stale=False)

    def unbind(self):
        self._reader = None
        self._channels = []
        self._cursor = 0
        self._min_ts = None
        self.chart.configure([], {})
        self.chart.set_caption("")
        self.table.set_rows([])
        self.table.set_droppable(())
        self._note.setText("")

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

    # -- per-tick ------------------------------------------------------------- #
    def tick(self, *, archived: bool, session_stale: bool):
        if self._reader is None or not self._channels:
            return
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
        self.chart.append_samples(fresh)
        self.chart.set_caption(
            "archived drive" if archived
            else "not advancing" if session_stale else "live")

    def set_failed(self, message: str):
        self.chart.set_caption("read failed")
        self._note.setText(f"read failed: {message}"[:120])

    def _on_span(self, idx: int):
        self.chart.set_span(WINDOW_SPANS[idx][1])

    # A layout change rebinds the pane; the page uses these to keep the user's
    # chosen window across that rebind instead of snapping back to the default.
    def span_text(self) -> str:
        return self._span_combo.currentText()

    def set_span_text(self, text: str):
        if any(label == text for label, _s in WINDOW_SPANS):
            self._span_combo.setCurrentText(text)
