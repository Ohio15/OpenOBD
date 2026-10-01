"""
OpenOBD GUI — HP-Tuners-style calibration viewer/editor + log overlay +
live/replay dashboard, for truck-mcp.

Tabs
----
  Editor      : tree of tables (by category) -> editable heatmapped grid with
                stock diff + optional log overlay (operation-count / mean-value).
  Scalars     : editable list of single-value parameters with stock diff.
  Log         : load a VCM Scanner CSV / plain CSV, see the regime report, and
                push a table overlay from any two channels.
  Dashboard   : gauges fed by a DataSource — CSV log replay, the OBDX Pro GT
                live link, or a truck-mcp drive-log session (tmsource): live
                sessions follow the store at ~1 Hz with tmstore's freshness
                states on every gauge; archived drives replay through the
                transport bar. Store access is read-only, never the serial port.
  Live Data   : session picker + a combined raw view off truck-mcp's drive-log
                store (sessions/*.tmsession.db), never the serial port: compact
                live tiles above a Chart vs. Time strip chart, read together.
  Active Tests: display-only vehicle-control state from truck-mcp's control
                journal — empty executable registry, outstanding-activation
                warnings. Commands nothing.

Settings → Preferences houses the dashboard-visual selector (HP Tuners
classic dial/bar cluster, or the modern dual-dial cluster); the choice is
persisted via QSettings and applies without rebinding the data source.

Run:  python -m openobd.app  [optional path to a .cal.json]
"""
from __future__ import annotations

import csv
import math
import os
import shutil
import sys
import tempfile
import threading
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QPointF, QRectF, QEvent, Signal
from PySide6.QtGui import (
    QAction, QColor, QIcon, QPainter, QPalette, QPen, QPolygonF, QFont,
    QLinearGradient, QRadialGradient, QUndoCommand, QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QTreeWidget, QTreeWidgetItem, QTableView, QTabWidget, QSplitter, QLabel,
    QComboBox, QCheckBox, QPushButton, QFileDialog, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QGroupBox, QLineEdit,
    QSlider, QListWidget, QListWidgetItem, QMenu, QDialog, QDialogButtonBox,
    QFormLayout,
)

import numpy as np
import pyqtgraph as pg

from . import __version__, editops
from .appsettings import app_settings
from .calspec import Calibration
from .diagui import DiagnosticsPage
from .livedata import ActiveTestsPage, LiveDataPage
from .logbin import (
    Log, Overlay, parse_csv, analyze_log, bin_log_to_table, detect_shift_points,
    CANONICAL, time_axis,
)

pg.setConfigOption("background", (28, 30, 34))
pg.setConfigOption("foreground", (200, 203, 208))
pg.setConfigOptions(antialias=True)
from .model import CalTableModel, HeatmapDelegate
from .transport import DataSource, LogReplaySource, GtDataSource, Sample
from . import tmsource, tmstore

# HP-Tuners-style cluster layout.
# Dials: key, label, unit, min, max, large?, display divisor, yellow-from, red-from
DIAL_SPECS = [
    ("maf",  "MAF",   "g/s", 0,   100, False, 1,    None, None),
    ("map",  "MAP",   "kPa", 0,   105, False, 1,    None, None),
    ("rpm",  "RPM",   "rpm", 0,  7000, True,  1000, 5500, 6200),
    ("vss",  "Speed", "mph", 0,   160, True,  1,    None, None),
    ("ect",  "ECT",   "°F",  100, 260, False, 1,    215,  235),
    ("iat",  "IAT",   "°F",  20,  180, False, 1,    120,  150),
]
# Bars: key, label, unit, min, max, red-above, red-below
BAR_SPECS = [
    ("knock_retard", "KR",      "°",   0,   10, 0.5,  None),
    ("spark",        "Advance", "°",  -10,  45, None, None),
    ("tps",          "TPS",     "%",   0,  100, None, None),
    ("app",          "Pedal",   "%",   0,  100, None, None),
    ("load",         "Load",    "%",   0,  100, None, None),
    ("stft",         "STFT",    "%",  -25,  25, 10,   -10),
    ("ltft",         "LTFT",    "%",  -25,  25, 10,   -10),
    ("fuel_press",   "Fuel",    "psi", 0,   75, None, 40),
    ("voltage",      "Volts",   "V",   10,  15, None, 12),
    ("tft",          "Trans",   "°F",  80, 300, 240,  None),
    ("gear",         "Gear",    "",    0,    6, None, None),
    ("ethanol",      "EtOH",    "%",   0,  100, None, None),
]

# Dashboard visuals selectable in Settings → Preferences (key → menu label).
# The key is what QSettings persists, so entries must never be renamed.
DASHBOARD_STYLES = {
    "classic": "HP Tuners classic — dial pair + bar cluster",
    "modern":  "Modern cluster — dual dial with tiles",
}

# "Modern cluster" layout: readout tiles left, big RPM/Speed dials flanking a
# center stack, temperature bar tiles right, and a ring-tile strip below.
MODERN_TILE_SPECS = [       # key, label, unit, min, max
    ("maf", "MAF", "g/s", 0, 100),
    ("map", "MAP", "kPa", 0, 105),
    ("tps", "TPS", "%",   0, 100),
]
MODERN_DIAL_SPECS = [       # key, label, sublabel, min, max, divisor, red-from
    ("rpm", "RPM",   "x1000 RPM", 0, 7000, 1000, 6200),
    ("vss", "Speed", "MPH",       0, 160,  1,    None),
]
MODERN_CENTER_SPECS = [     # key, label, unit
    ("gear",    "GEAR",    ""),
    ("ambient", "OUTSIDE", "°F"),
    ("voltage", "VOLTS",   "V"),
]
MODERN_BAR_SPECS = [        # key, label, unit, min, max, red-above
    ("ect", "ECT",   "°F", 100, 260, 235),
    ("iat", "IAT",   "°F", 20,  180, 150),
    ("tft", "TRANS", "°F", 80,  300, 240),
]
MODERN_RING_SPECS = [       # key, label, unit, min, max, red-above, red-below
    ("knock_retard", "KR",      "°",   0,   10, 0.5,  None),
    ("spark",        "ADVANCE", "°",  -10,  45, None, None),
    ("stft",         "STFT",    "%",  -25,  25, 10,   -10),
    ("ltft",         "LTFT",    "%",  -25,  25, 10,   -10),
    ("fuel_level",   "FUEL",    "%",   0,  100, None, 10),
    ("fuel_press",   "FUEL P",  "psi", 0,   75, None, 40),
    ("load",         "LOAD",    "%",   0,  100, None, None),
    ("app",          "PEDAL",   "%",   0,  100, None, None),
    ("ethanol",      "ETOH",    "%",   0,  100, None, None),
]


# Column labels for recorded datalogs. Chosen so logbin.map_channel maps them
# back to the same canonical key when the CSV is reloaded in Log Analysis.
RECORD_LABELS = {
    "rpm": "Engine Speed (RPM)",
    "vss": "Vehicle Speed (mph)",
    "map": "Manifold Absolute Pressure (kPa)",
    "maf": "Mass Air Flow (g/s)",
    "load": "Engine Load (%)",
    "tps": "Throttle Position (%)",
    "app": "Accelerator Pedal (%)",
    "cmd_throttle": "Commanded Throttle (%)",
    "spark": "Spark Advance (deg)",
    "ect": "Engine Coolant Temp (F)",
    "iat": "Intake Air Temp (F)",
    "stft": "Short Term Fuel Trim (%)",
    "ltft": "Long Term Fuel Trim (%)",
    "eq_ratio": "Commanded Equivalence Ratio",
    "fuel_press": "Fuel Pressure (psi)",
    "fuel_level": "Fuel Level (%)",
    "voltage": "Module Voltage (V)",
    "baro": "Barometric Pressure (kPa)",
    "ambient": "Ambient Air Temp (F)",
    "ethanol": "Ethanol (%)",
}
RECORD_ORDER = ["rpm", "vss", "map", "maf", "load", "tps", "app",
                "cmd_throttle", "spark", "ect", "iat", "stft", "ltft",
                "eq_ratio", "fuel_press", "fuel_level", "voltage", "baro",
                "ambient", "ethanol"]


# --------------------------------------------------------------------------- #
# HP-Tuners-style gauges: round dials with needles and vertical bars. Pure
# QPainter, no deps.
# --------------------------------------------------------------------------- #
def _nice_step(span: float, target: int = 8) -> float:
    """A 1/2/2.5/5×10^k step giving roughly `target` major ticks."""
    raw = span / max(1, target)
    mag = 10.0 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for m in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= m * mag:
            return m * mag
    return 10.0 * mag


class GaugeBase(QWidget):
    """Shared value/min-max/availability plumbing for dials and bars."""

    def __init__(self, key, label, unit, vmin, vmax):
        super().__init__()
        self.key = key
        self.label = label
        self.unit = unit
        self.vmin = float(vmin)
        self.vmax = float(vmax)
        self.value: Optional[float] = None
        self.min_seen: Optional[float] = None
        self.max_seen: Optional[float] = None
        self.available = True   # False = bound source doesn't offer this channel
        # tmstore freshness state (None for sources without the vocabulary).
        # A carried/stale/archived value must not render like a live needle.
        self.state: Optional[str] = None
        self.setToolTip(f"{label} — click to reset min/max capture")

    def reset(self):
        self.value = None
        self.min_seen = self.max_seen = None
        self.state = None
        self.update()

    # Caption + accent per non-live state; live/no-vocabulary states draw plain.
    STATE_BADGES = {
        "carried":  ("carried",       QColor(235, 170, 60)),
        "stale":    ("not advancing", QColor(235, 170, 60)),
        "archive":  ("archived",      QColor(90, 126, 195)),
        "error":    ("module error",  QColor(205, 60, 50)),
        "failed":   ("read failed",   QColor(205, 60, 50)),
        "badvalue": ("bad value",     QColor(192, 86, 143)),
        "unavail":  ("no data",       QColor(90, 126, 195)),
        "notread":  ("not read",      QColor(95, 100, 110)),
    }

    def set_state(self, state: Optional[str]):
        if state != self.state:
            self.state = state
            self.update()

    def _state_badge(self):
        """(caption, accent color) for the current state, or None to draw plain.
        Unknown states fail CLOSED: only fresh/None may render live-styled —
        a state this widget doesn't recognize must not pass for a live value."""
        if not self.available or self.state in (None, "fresh"):
            return None
        return self.STATE_BADGES.get(
            self.state, (str(self.state)[:14] or "unknown", QColor(205, 60, 50)))

    def set_available(self, on: bool):
        self.available = on
        self.update()

    def set_value(self, v: Optional[float]):
        self.value = v
        if v is not None:
            self.min_seen = v if self.min_seen is None else min(self.min_seen, v)
            self.max_seen = v if self.max_seen is None else max(self.max_seen, v)
        self.update()

    def mousePressEvent(self, _event):
        self.min_seen = self.max_seen = None
        self.update()

    def _frac(self, v: float) -> float:
        span = (self.vmax - self.vmin) or 1.0
        return max(0.0, min(1.0, (v - self.vmin) / span))


class DialGauge(GaugeBase):
    """Round analog dial: radial blue face, tick ring, yellow/red zones, a
    needle (parked at min until data arrives), digital readout."""

    SWEEP_START = 225.0   # degrees, math orientation (0=east, CCW+)
    SWEEP = 270.0         # clockwise sweep from SWEEP_START

    def __init__(self, key, label, unit, vmin, vmax, large=False,
                 divisor=1.0, yellow_from=None, red_from=None):
        super().__init__(key, label, unit, vmin, vmax)
        self.divisor = float(divisor)
        self.yellow_from = yellow_from
        self.red_from = red_from
        side = 240 if large else 140
        self.setMinimumSize(side, side)

    def _angle(self, v: float) -> float:
        return self.SWEEP_START - self.SWEEP * self._frac(v)

    def _pt(self, cx, cy, r, ang_deg):
        a = math.radians(ang_deg)
        return QPointF(cx + r * math.cos(a), cy - r * math.sin(a))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        side = min(w, h)
        cx, cy = w / 2.0, h / 2.0
        r = side / 2.0 - 3

        dim = not self.available

        # bezel + face
        p.setPen(QPen(QColor(65, 70, 78) if dim else QColor(185, 190, 198),
                      max(2.0, side * 0.018)))
        grad = QRadialGradient(cx, cy, r)
        if dim:
            grad.setColorAt(0.0, QColor(30, 36, 48))
            grad.setColorAt(1.0, QColor(14, 17, 24))
        else:
            grad.setColorAt(0.0, QColor(38, 74, 150))
            grad.setColorAt(0.55, QColor(24, 48, 104))
            grad.setColorAt(1.0, QColor(8, 14, 30))
        p.setBrush(grad)
        p.drawEllipse(QPointF(cx, cy), r, r)

        # colored zones (arcs just inside the tick ring)
        arc_r = r * 0.86
        arc_rect = QRectF(cx - arc_r, cy - arc_r, 2 * arc_r, 2 * arc_r)
        for start_v, color in ((self.yellow_from, QColor(235, 200, 60)),
                               (self.red_from, QColor(215, 45, 40))):
            if start_v is None or dim:
                continue
            a0 = self._angle(self.vmax)
            a1 = self._angle(max(start_v, self.vmin))
            p.setPen(QPen(color, max(3.0, side * 0.03), Qt.SolidLine, Qt.FlatCap))
            p.setBrush(Qt.NoBrush)
            p.drawArc(arc_rect, int(a0 * 16), int((a1 - a0) * 16))

        # ticks + numerals
        step = _nice_step(self.vmax - self.vmin)
        minor = step / 4.0
        tick_pen_c = QColor(120, 126, 136) if dim else QColor(238, 240, 244)
        num_font = QFont()
        num_font.setPointSizeF(max(6.0, side * 0.052))
        num_font.setBold(True)
        p.setFont(num_font)
        v = self.vmin
        while v <= self.vmax + 1e-9:
            ang = self._angle(v)
            is_major = abs((v - self.vmin) % step) < 1e-6 \
                or abs(step - (v - self.vmin) % step) < 1e-6
            ln = r * (0.13 if is_major else 0.065)
            p.setPen(QPen(tick_pen_c, max(1.2, side * (0.012 if is_major else 0.007))))
            p.drawLine(self._pt(cx, cy, r * 0.97, ang),
                       self._pt(cx, cy, r * 0.97 - ln, ang))
            if is_major:
                lbl = f"{v / self.divisor:g}"
                lp = self._pt(cx, cy, r * 0.68, ang)
                rect = QRectF(lp.x() - side * 0.09, lp.y() - side * 0.05,
                              side * 0.18, side * 0.1)
                p.setPen(tick_pen_c)
                p.drawText(rect, Qt.AlignCenter, lbl)
            v += minor

        # unit + name
        p.setPen(QColor(120, 126, 136) if dim else QColor(200, 208, 220))
        f2 = QFont(); f2.setPointSizeF(max(6.0, side * 0.055)); p.setFont(f2)
        p.drawText(QRectF(cx - r, cy - r * 0.52, 2 * r, side * 0.12),
                   Qt.AlignCenter, self.unit)
        p.drawText(QRectF(cx - r, cy + r * 0.62, 2 * r, side * 0.13),
                   Qt.AlignCenter, self.label)

        # digital readout + min/max
        p.setPen(QColor(130, 136, 146) if dim else QColor(245, 246, 248))
        f3 = QFont(); f3.setPointSizeF(max(7.0, side * 0.07)); f3.setBold(True)
        p.setFont(f3)
        txt = "—" if self.value is None else f"{self.value:g}"
        p.drawText(QRectF(cx - r, cy + r * 0.30, 2 * r, side * 0.16),
                   Qt.AlignCenter, txt)
        if self.max_seen is not None:
            p.setPen(QColor(150, 155, 162))
            f4 = QFont(); f4.setPointSizeF(max(5.5, side * 0.042)); p.setFont(f4)
            p.drawText(QRectF(cx - r, cy + r * 0.47, 2 * r, side * 0.1),
                       Qt.AlignCenter,
                       f"▼{self.min_seen:g} ▲{self.max_seen:g}")

        # freshness badge (tmstore vocabulary) — a carried/stale/archived value
        # must announce itself, not render like a live measurement
        badge = self._state_badge()
        if badge is not None:
            caption, accent = badge
            p.setPen(accent)
            fb = QFont(); fb.setPointSizeF(max(5.5, side * 0.045)); fb.setBold(True)
            p.setFont(fb)
            p.drawText(QRectF(cx - r, cy - r * 0.78, 2 * r, side * 0.11),
                       Qt.AlignCenter, caption)

        # needle (parked at vmin when no data yet)
        needle_v = self.vmin if self.value is None else self.value
        ang = self._angle(needle_v)
        tip = self._pt(cx, cy, r * 0.88, ang)
        left = self._pt(cx, cy, r * 0.07, ang + 100)
        right = self._pt(cx, cy, r * 0.07, ang - 100)
        tail = self._pt(cx, cy, r * 0.16, ang + 180)
        needle = QPolygonF([tip, left, tail, right])
        p.setPen(Qt.NoPen)
        if dim or self.value is None:
            needle_c = QColor(105, 110, 120)
        elif badge is not None:
            needle_c = badge[1]
        else:
            needle_c = QColor(240, 90, 60)
        p.setBrush(needle_c)
        p.drawPolygon(needle)
        # hub
        hub = QRadialGradient(cx, cy, r * 0.14)
        hub.setColorAt(0.0, QColor(210, 214, 220))
        hub.setColorAt(1.0, QColor(90, 95, 104))
        p.setBrush(hub)
        p.drawEllipse(QPointF(cx, cy), r * 0.11, r * 0.11)
        p.end()


class BarGauge(GaugeBase):
    """Vertical bar with a blue gradient fill, side scale, and red zones —
    the HP Tuners bar-cluster look."""

    def __init__(self, key, label, unit, vmin, vmax,
                 red_above=None, red_below=None):
        super().__init__(key, label, unit, vmin, vmax)
        self.red_above = red_above
        self.red_below = red_below
        self.setMinimumSize(66, 170)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        dim = not self.available

        badge = self._state_badge()

        # name (top) and value (bottom); the value takes the freshness accent so
        # a carried/stale reading can't pass for live even at bar-gauge size
        p.setPen(QColor(120, 126, 136) if dim else QColor(225, 228, 232))
        f = QFont(); f.setPointSizeF(8.5); f.setBold(True); p.setFont(f)
        p.drawText(QRectF(0, 2, w, 14), Qt.AlignCenter, self.label)
        if badge is not None and not dim:
            p.setPen(badge[1])
        txt = "—" if self.value is None else f"{self.value:g}"
        p.drawText(QRectF(0, h - 16, w, 14), Qt.AlignCenter, txt)

        # bar frame
        top, bot = 20, h - 20
        bar_w = max(18, int(w * 0.34))
        bx = 6
        p.setPen(QPen(QColor(150, 155, 164) if not dim else QColor(70, 75, 84), 1.4))
        p.setBrush(QColor(12, 14, 20))
        p.drawRect(QRectF(bx, top, bar_w, bot - top))

        # fill (amber/accented when the value is not a live measurement)
        if self.value is not None and not dim:
            frac = self._frac(self.value)
            fill_top = bot - (bot - top) * frac
            grad = QLinearGradient(0, bot, 0, top)
            if badge is not None:
                a = badge[1]
                grad.setColorAt(0.0, a.darker(220))
                grad.setColorAt(0.7, a)
                grad.setColorAt(1.0, a.lighter(130))
            else:
                grad.setColorAt(0.0, QColor(20, 45, 110))
                grad.setColorAt(0.7, QColor(60, 120, 220))
                grad.setColorAt(1.0, QColor(130, 185, 255))
            p.setPen(Qt.NoPen)
            p.setBrush(grad)
            p.drawRect(QRectF(bx + 1.5, fill_top, bar_w - 3, bot - fill_top))

        # side scale: ticks, labels, red zones
        sx = bx + bar_w + 3
        step = _nice_step(self.vmax - self.vmin, target=5)
        tick_c = QColor(110, 116, 126) if dim else QColor(225, 165, 60)
        f2 = QFont(); f2.setPointSizeF(6.5); p.setFont(f2)

        def y_of(v):
            return bot - (bot - top) * self._frac(v)

        for zone_v, above in ((self.red_above, True), (self.red_below, False)):
            if zone_v is None or dim:
                continue
            y0 = y_of(zone_v)
            y1 = top if above else bot
            p.setPen(QPen(QColor(215, 45, 40), 2.5))
            p.drawLine(QPointF(sx, y0), QPointF(sx, y1))

        v = self.vmin
        while v <= self.vmax + 1e-9:
            y = y_of(v)
            p.setPen(QPen(tick_c, 1.0))
            p.drawLine(QPointF(sx + 3, y), QPointF(sx + 8, y))
            p.setPen(tick_c)
            p.drawText(QRectF(sx + 9, y - 6, w - sx - 9, 12),
                       Qt.AlignLeft | Qt.AlignVCenter, f"{v:g}")
            v += step
        p.end()


# --------------------------------------------------------------------------- #
# "Modern cluster" gauges (Settings → Preferences → Dashboard visual): a dark
# dual-dial cluster with digital readout tiles. Same GaugeBase plumbing, so
# availability dimming and tmstore freshness badges behave identically.
# --------------------------------------------------------------------------- #
_M_TILE = QColor(16, 20, 27)
_M_TILE_DIM = QColor(12, 14, 18)
_M_EDGE = QColor(46, 54, 68)
_M_EDGE_DIM = QColor(30, 34, 42)
_M_TEXT = QColor(235, 239, 245)
_M_DIM = QColor(118, 127, 140)
_M_BLUE = QColor(58, 140, 235)
_M_RED = QColor(222, 58, 48)


def _m_fmt(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:g}"


def _m_fit(font: QFont, text: str, avail_w: float) -> QFont:
    """Shrink `font` (never grow it) until `text` fits in avail_w pixels."""
    from PySide6.QtGui import QFontMetricsF
    wpx = QFontMetricsF(font).horizontalAdvance(text)
    if wpx > avail_w > 0:
        font.setPointSizeF(max(6.0, font.pointSizeF() * avail_w / wpx))
    return font


class ModernTile(GaugeBase):
    """Rounded readout tile: small caps label, large digital value + unit."""

    def __init__(self, key, label, unit, vmin=0.0, vmax=1.0, centered=False):
        super().__init__(key, label, unit, vmin, vmax)
        self.centered = centered
        self.setMinimumSize(96, 56)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        dim = not self.available
        p.setPen(QPen(_M_EDGE_DIM if dim else _M_EDGE, 1.2))
        p.setBrush(_M_TILE_DIM if dim else _M_TILE)
        p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 9, 9)

        align = Qt.AlignHCenter if self.centered else Qt.AlignLeft
        p.setPen(_M_DIM)
        f = QFont(); f.setPointSizeF(7.5); f.setBold(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
        p.setFont(f)
        p.drawText(QRectF(10, 5, w - 20, 13), align | Qt.AlignVCenter, self.label)

        badge = self._state_badge()
        p.setPen(QColor(120, 126, 136) if dim
                 else (badge[1] if badge else _M_TEXT))
        f2 = QFont()
        f2.setPointSizeF(max(11.0, min(h * 0.28, 26.0)))
        f2.setBold(True)
        txt = _m_fmt(self.value) + (f" {self.unit}" if self.unit else "")
        p.setFont(_m_fit(f2, txt, w - 20))
        p.drawText(QRectF(10, 16, w - 20, h - (32 if badge else 20)),
                   align | Qt.AlignVCenter, txt)
        if badge is not None:
            p.setPen(badge[1])
            f3 = QFont(); f3.setPointSizeF(6.5); p.setFont(f3)
            p.drawText(QRectF(10, h - 16, w - 20, 12),
                       align | Qt.AlignVCenter, badge[0])
        p.end()


class ModernBarTile(GaugeBase):
    """Tile with a digital readout and a horizontal blue bar + red zone."""

    def __init__(self, key, label, unit, vmin, vmax, red_above=None):
        super().__init__(key, label, unit, vmin, vmax)
        self.red_above = red_above
        self.setMinimumSize(120, 58)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        dim = not self.available
        badge = self._state_badge()
        p.setPen(QPen(_M_EDGE_DIM if dim else _M_EDGE, 1.2))
        p.setBrush(_M_TILE_DIM if dim else _M_TILE)
        p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 9, 9)

        # top row: label left, big value + unit right
        p.setPen(_M_DIM)
        f = QFont(); f.setPointSizeF(7.5); f.setBold(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
        p.setFont(f)
        p.drawText(QRectF(10, 4, w - 20, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   self.label)
        p.setPen(QColor(120, 126, 136) if dim
                 else (badge[1] if badge else _M_TEXT))
        f2 = QFont()
        f2.setPointSizeF(max(10.0, min(h * 0.22, 15.0)))
        f2.setBold(True)
        txt = _m_fmt(self.value) + (f" {self.unit}" if self.unit else "")
        p.setFont(_m_fit(f2, txt, max(40.0, w - 90.0)))
        p.drawText(QRectF(10, 2, w - 20, 24), Qt.AlignRight | Qt.AlignVCenter,
                   txt)

        # middle band: badge caption (left) + scale end labels
        f3 = QFont(); f3.setPointSizeF(6.0); p.setFont(f3)
        if badge is not None and not dim:
            p.setPen(badge[1])
            p.drawText(QRectF(10, h - 26, w - 20, 11),
                       Qt.AlignLeft | Qt.AlignVCenter, badge[0])
        else:
            p.setPen(_M_DIM)
            p.drawText(QRectF(10, h - 26, 60, 11),
                       Qt.AlignLeft | Qt.AlignVCenter, f"{self.vmin:g}")
        p.setPen(_M_DIM)
        p.drawText(QRectF(w - 70, h - 26, 60, 11),
                   Qt.AlignRight | Qt.AlignVCenter, f"{self.vmax:g}")

        # bar: dark track, red zone, blue fill (badge accent when not live)
        by, bx0, bx1 = h - 14.0, 10.0, w - 10.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(28, 33, 42))
        p.drawRoundedRect(QRectF(bx0, by, bx1 - bx0, 6), 3, 3)
        if self.red_above is not None and not dim:
            x = bx0 + (bx1 - bx0) * self._frac(self.red_above)
            p.setBrush(QColor(120, 30, 26))
            p.drawRoundedRect(QRectF(x, by, bx1 - x, 6), 3, 3)
        if self.value is not None and not dim:
            in_red = self.red_above is not None and self.value >= self.red_above
            p.setBrush(badge[1] if badge else (_M_RED if in_red else _M_BLUE))
            x = bx0 + (bx1 - bx0) * self._frac(self.value)
            p.drawRoundedRect(QRectF(bx0, by, max(4.0, x - bx0), 6), 3, 3)
        p.end()


class ModernRing(GaugeBase):
    """Circular mini-gauge: 270° value arc, digital value inside, label below."""

    START = 225.0
    SPAN = 270.0

    def __init__(self, key, label, unit, vmin, vmax,
                 red_above=None, red_below=None):
        super().__init__(key, label, unit, vmin, vmax)
        self.red_above = red_above
        self.red_below = red_below
        self.setMinimumSize(74, 96)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        dim = not self.available
        badge = self._state_badge()
        d = max(30.0, min(w - 12.0, h - 30.0))
        cx, cy = w / 2.0, 6.0 + d / 2.0
        rect = QRectF(cx - d / 2, cy - d / 2, d, d)
        pen_w = max(3.0, d * 0.07)
        p.setPen(QPen(QColor(22, 25, 31) if dim else QColor(34, 40, 50),
                      pen_w, Qt.SolidLine, Qt.FlatCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(rect, int(self.START * 16), int(-self.SPAN * 16))
        if self.value is not None and not dim:
            in_red = ((self.red_above is not None and self.value >= self.red_above)
                      or (self.red_below is not None and self.value <= self.red_below))
            arc_c = badge[1] if badge else (_M_RED if in_red else _M_BLUE)
            p.setPen(QPen(arc_c, pen_w, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(rect, int(self.START * 16),
                      int(-self.SPAN * 16 * self._frac(self.value)))

        p.setPen(QColor(120, 126, 136) if dim
                 else (badge[1] if badge else _M_TEXT))
        f = QFont(); f.setPointSizeF(max(8.0, d * 0.20)); f.setBold(True)
        p.setFont(f)
        p.drawText(rect, Qt.AlignCenter, _m_fmt(self.value))

        # caption: freshness badge when present, otherwise "LABEL unit"
        if badge is not None and not dim:
            p.setPen(badge[1])
            cap = badge[0]
        else:
            p.setPen(_M_DIM)
            cap = f"{self.label} {self.unit}".strip()
        f2 = QFont(); f2.setPointSizeF(7.0); f2.setBold(True)
        f2.setLetterSpacing(QFont.AbsoluteSpacing, 0.5)
        p.setFont(f2)
        p.drawText(QRectF(0, cy + d / 2 + 3, w, 14),
                   Qt.AlignHCenter | Qt.AlignTop, cap)
        p.end()


class ModernDial(GaugeBase):
    """Large modern dial: dark face, blue value arc, thin floating needle, and
    a big digital readout in the center (RPM / Speed)."""

    SWEEP_START = 225.0   # degrees, math orientation (0=east, CCW+)
    SWEEP = 270.0         # clockwise sweep from SWEEP_START

    def __init__(self, key, label, sublabel, vmin, vmax,
                 divisor=1.0, red_from=None):
        # the unit slot carries the sublabel drawn under the digits
        super().__init__(key, label, sublabel, vmin, vmax)
        self.divisor = float(divisor)
        self.red_from = red_from
        self.setMinimumSize(220, 220)

    def _angle(self, v: float) -> float:
        return self.SWEEP_START - self.SWEEP * self._frac(v)

    def _pt(self, cx, cy, r, ang_deg):
        a = math.radians(ang_deg)
        return QPointF(cx + r * math.cos(a), cy - r * math.sin(a))

    def _fmt_digits(self) -> str:
        if self.value is None:
            return "—"
        if self.divisor > 1:
            return f"{self.value / self.divisor:.1f}"
        return f"{self.value:.0f}"

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        side = min(w, h)
        cx, cy = w / 2.0, h / 2.0
        r = side / 2.0 - 3
        dim = not self.available
        badge = self._state_badge()

        # face + thin bezel
        p.setPen(QPen(_M_EDGE_DIM if dim else _M_EDGE, max(1.5, side * 0.008)))
        grad = QRadialGradient(cx, cy, r)
        if dim:
            grad.setColorAt(0.0, QColor(16, 18, 23))
            grad.setColorAt(1.0, QColor(9, 10, 13))
        else:
            grad.setColorAt(0.0, QColor(21, 26, 35))
            grad.setColorAt(0.75, QColor(13, 16, 22))
            grad.setColorAt(1.0, QColor(7, 9, 13))
        p.setBrush(grad)
        p.drawEllipse(QPointF(cx, cy), r, r)

        # sweep track + value arc
        arc_r = r * 0.90
        arc_rect = QRectF(cx - arc_r, cy - arc_r, 2 * arc_r, 2 * arc_r)
        arc_w = max(3.0, side * 0.028)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(22, 25, 31) if dim else QColor(30, 37, 48),
                      arc_w, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(arc_rect, int(self.SWEEP_START * 16), int(-self.SWEEP * 16))
        if self.value is not None and not dim:
            arc_c = badge[1] if badge else _M_BLUE
            p.setPen(QPen(arc_c, arc_w, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(arc_rect, int(self.SWEEP_START * 16),
                      int(-self.SWEEP * 16 * self._frac(self.value)))

        # red zone just inside the numeral ring
        if self.red_from is not None and not dim:
            zr = r * 0.97
            z_rect = QRectF(cx - zr, cy - zr, 2 * zr, 2 * zr)
            a0 = self._angle(max(self.red_from, self.vmin))
            a1 = self._angle(self.vmax)
            p.setPen(QPen(_M_RED, max(2.5, side * 0.018),
                          Qt.SolidLine, Qt.FlatCap))
            p.drawArc(z_rect, int(a0 * 16), int((a1 - a0) * 16))

        # ticks + numerals
        step = _nice_step(self.vmax - self.vmin)
        minor = step / 4.0
        tick_c = QColor(90, 96, 106) if dim else QColor(205, 212, 222)
        num_font = QFont()
        num_font.setPointSizeF(max(6.0, side * 0.048))
        num_font.setBold(True)
        p.setFont(num_font)
        v = self.vmin
        while v <= self.vmax + 1e-9:
            ang = self._angle(v)
            is_major = abs((v - self.vmin) % step) < 1e-6 \
                or abs(step - (v - self.vmin) % step) < 1e-6
            ln = r * (0.09 if is_major else 0.045)
            p.setPen(QPen(tick_c,
                          max(1.0, side * (0.010 if is_major else 0.006))))
            p.drawLine(self._pt(cx, cy, r * 0.96, ang),
                       self._pt(cx, cy, r * 0.96 - ln, ang))
            if is_major:
                in_red = self.red_from is not None and v >= self.red_from
                p.setPen(_M_RED if (in_red and not dim) else tick_c)
                lp = self._pt(cx, cy, r * 0.74, ang)
                p.drawText(QRectF(lp.x() - side * 0.09, lp.y() - side * 0.05,
                                  side * 0.18, side * 0.1),
                           Qt.AlignCenter, f"{v / self.divisor:g}")
            v += minor

        # big digital readout + sublabel
        p.setPen(QColor(120, 126, 136) if dim
                 else (badge[1] if badge else _M_TEXT))
        fd = QFont(); fd.setPointSizeF(max(12.0, side * 0.155)); fd.setBold(True)
        p.setFont(fd)
        p.drawText(QRectF(cx - r, cy - side * 0.16, 2 * r, side * 0.24),
                   Qt.AlignCenter, self._fmt_digits())
        p.setPen(_M_DIM)
        fs = QFont(); fs.setPointSizeF(max(6.5, side * 0.042)); fs.setBold(True)
        p.setFont(fs)
        p.drawText(QRectF(cx - r, cy + side * 0.09, 2 * r, side * 0.09),
                   Qt.AlignCenter, self.unit)

        # min/max capture + freshness badge
        if self.max_seen is not None:
            p.setPen(QColor(130, 136, 146))
            f4 = QFont(); f4.setPointSizeF(max(5.5, side * 0.036)); p.setFont(f4)
            p.drawText(QRectF(cx - r, cy + r * 0.52, 2 * r, side * 0.09),
                       Qt.AlignCenter,
                       f"▼{self.min_seen:g} ▲{self.max_seen:g}")
        if badge is not None:
            p.setPen(badge[1])
            fb = QFont(); fb.setPointSizeF(max(5.5, side * 0.040)); fb.setBold(True)
            p.setFont(fb)
            p.drawText(QRectF(cx - r, cy - r * 0.62, 2 * r, side * 0.10),
                       Qt.AlignCenter, badge[0])

        # thin floating needle (outer segment only, keeps the digits clear);
        # parked at vmin when no data yet
        needle_v = self.vmin if self.value is None else self.value
        ang = self._angle(needle_v)
        if dim or self.value is None:
            needle_c = QColor(80, 85, 94)
        elif badge is not None:
            needle_c = badge[1]
        else:
            needle_c = _M_RED
        p.setPen(QPen(needle_c, max(2.0, side * 0.012),
                      Qt.SolidLine, Qt.RoundCap))
        p.drawLine(self._pt(cx, cy, r * 0.55, ang),
                   self._pt(cx, cy, r * 0.88, ang))
        p.end()


class Dashboard(QWidget):
    # GT connect runs on a worker thread (serial open + first poll can take
    # seconds); these marshal the result back onto the GUI thread.
    gt_ready = Signal(object)
    gt_error = Signal(str)

    def __init__(self, style: str = "classic"):
        super().__init__()
        self.style = style if style in DASHBOARD_STYLES else "classic"
        self.source: Optional[DataSource] = None
        self.gauges: dict[str, GaugeBase] = {}
        self.recording = False
        self._rec_file = None          # open csv file the recorder streams to
        self._rec_writer = None
        self._rec_count = 0
        self._rec_keys = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.gt_ready.connect(self._on_gt_ready)
        self.gt_error.connect(self._on_gt_error)

        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.status = QLabel("No source. Load a log, then Start replay.")
        self.btn_start = QPushButton("▶ Start")
        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_stop.setEnabled(False)
        self.btn_connect = QPushButton("🔌 Connect GT Pro")
        self.btn_connect.clicked.connect(self.connect_gt)
        self.btn_tm = QPushButton("🗄 Truck Session")
        self.btn_tm.clicked.connect(self._pick_tm_session)
        self.btn_record = QPushButton("⏺ Record")
        self.btn_record.clicked.connect(self.toggle_record)
        bar.addWidget(self.status, 1)
        bar.addWidget(self.btn_connect)
        bar.addWidget(self.btn_tm)
        bar.addWidget(self.btn_record)
        bar.addWidget(self.btn_start)
        bar.addWidget(self.btn_stop)
        root.addLayout(bar)

        # replay transport controls (hidden for live sources)
        self.replay_ctl = QWidget()
        rb = QHBoxLayout(self.replay_ctl)
        rb.setContentsMargins(0, 0, 0, 0)
        self.btn_pause = QPushButton("⏸ Pause")
        self.btn_pause.clicked.connect(self._toggle_pause)
        rb.addWidget(self.btn_pause)
        rb.addWidget(QLabel("Speed:"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.5×", "1×", "2×", "4×", "8×"])
        self.speed_combo.setCurrentText("4×")
        self.speed_combo.currentTextChanged.connect(self._on_speed)
        rb.addWidget(self.speed_combo)
        self.pos_slider = QSlider(Qt.Horizontal)
        self.pos_slider.sliderReleased.connect(self._on_seek)
        self.pos_slider.sliderMoved.connect(
            lambda v: self.time_label.setText(self._fmt_pos(v / 10.0)))
        rb.addWidget(self.pos_slider, 1)
        self.time_label = QLabel("0.0 / 0.0s")
        rb.addWidget(self.time_label)
        self.replay_ctl.setVisible(False)
        root.addWidget(self.replay_ctl)

        self.cluster_host = QWidget()
        self._cluster_lay = QVBoxLayout(self.cluster_host)
        self._cluster_lay.setContentsMargins(0, 0, 0, 0)
        self._cluster: Optional[QWidget] = None
        root.addWidget(self.cluster_host, 1)

        # The full cluster is visible from the start:
        # every gauge shows "—" / a parked needle until data arrives.
        self._rebuild_cluster()

    # -- dashboard visuals (Settings → Preferences) --------------------------- #
    def set_style(self, style: str):
        """Switch the dashboard visual. The bound source stays bound: the new
        cluster picks values back up from latest() on the next tick, and
        freshness states are re-applied there too."""
        if style not in DASHBOARD_STYLES or style == self.style:
            return
        self.style = style
        self._rebuild_cluster()

    def _rebuild_cluster(self):
        if self._cluster is not None:
            self._cluster_lay.removeWidget(self._cluster)
            self._cluster.deleteLater()
        self.gauges.clear()
        build = (self._build_modern_cluster if self.style == "modern"
                 else self._build_classic_cluster)
        self._cluster = build()
        self._cluster_lay.addWidget(self._cluster)
        if self.source is not None:
            avail = set(self.source.channels())
            for key, g in self.gauges.items():
                g.set_available(key in avail)

    def _build_classic_cluster(self) -> QWidget:
        host = QWidget()
        cluster = QVBoxLayout(host)
        cluster.setContentsMargins(0, 0, 0, 0)
        cluster.setSpacing(4)

        dials = {}
        for key, label, unit, lo, hi, large, div, yel, red in DIAL_SPECS:
            dials[key] = DialGauge(key, label, unit, lo, hi, large=large,
                                   divisor=div, yellow_from=yel, red_from=red)
            self.gauges[key] = dials[key]

        # dial row: small pairs flank the two large dials, HP Tuners layout
        dial_row = QHBoxLayout()
        left_col = QVBoxLayout()
        left_col.addWidget(dials["maf"]); left_col.addWidget(dials["map"])
        dial_row.addLayout(left_col, 1)
        dial_row.addWidget(dials["rpm"], 2)
        dial_row.addWidget(dials["vss"], 2)
        right_col = QVBoxLayout()
        right_col.addWidget(dials["ect"]); right_col.addWidget(dials["iat"])
        dial_row.addLayout(right_col, 1)
        cluster.addLayout(dial_row, 3)

        bar_row = QHBoxLayout()
        bar_row.setSpacing(4)
        for key, label, unit, lo, hi, ra, rb in BAR_SPECS:
            g = BarGauge(key, label, unit, lo, hi, red_above=ra, red_below=rb)
            self.gauges[key] = g
            bar_row.addWidget(g, 1)
        cluster.addLayout(bar_row, 2)
        return host

    def _build_modern_cluster(self) -> QWidget:
        host = QWidget()
        pal = host.palette()
        pal.setColor(QPalette.Window, QColor(9, 11, 15))
        host.setAutoFillBackground(True)
        host.setPalette(pal)
        cluster = QVBoxLayout(host)
        cluster.setContentsMargins(8, 8, 8, 8)
        cluster.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)

        left = QVBoxLayout()
        left.setSpacing(6)
        for key, label, unit, lo, hi in MODERN_TILE_SPECS:
            g = ModernTile(key, label, unit, lo, hi)
            self.gauges[key] = g
            left.addWidget(g, 1)
        top.addLayout(left, 2)

        dials = []
        for key, label, sub, lo, hi, div, red in MODERN_DIAL_SPECS:
            g = ModernDial(key, label, sub, lo, hi, divisor=div, red_from=red)
            self.gauges[key] = g
            dials.append(g)
        top.addWidget(dials[0], 4)

        center = QVBoxLayout()
        center.setSpacing(6)
        for key, label, unit in MODERN_CENTER_SPECS:
            g = ModernTile(key, label, unit, centered=True)
            self.gauges[key] = g
            center.addWidget(g, 1)
        top.addLayout(center, 2)

        top.addWidget(dials[1], 4)

        right = QVBoxLayout()
        right.setSpacing(6)
        for key, label, unit, lo, hi, ra in MODERN_BAR_SPECS:
            g = ModernBarTile(key, label, unit, lo, hi, red_above=ra)
            self.gauges[key] = g
            right.addWidget(g, 1)
        top.addLayout(right, 3)
        cluster.addLayout(top, 3)

        ring_row = QHBoxLayout()
        ring_row.setSpacing(6)
        for key, label, unit, lo, hi, ra, rb in MODERN_RING_SPECS:
            g = ModernRing(key, label, unit, lo, hi,
                           red_above=ra, red_below=rb)
            self.gauges[key] = g
            ring_row.addWidget(g, 1)
        cluster.addLayout(ring_row, 1)
        return host

    def _teardown_source(self):
        """Stop and drop the current source. Always called before a new one is
        bound so a live GT poll thread can't keep running invisibly."""
        if self.recording:
            self._finalize_record()
        self.timer.stop()
        if self.source:
            try:
                self.source.stop()
            except Exception:
                pass
        self._demote_live_badges()
        self.source = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def bind_source(self, source: DataSource):
        self._teardown_source()
        self.source = source
        avail = set(source.channels())
        # The cluster is permanent — reset every gauge and dim the ones this
        # source can't feed instead of rebuilding a filtered grid.
        for key, g in self.gauges.items():
            g.reset()
            g.set_available(key in avail)
        n = sum(1 for k in self.gauges if k in avail)
        self.status.setText(f"Source bound: {n} of {len(self.gauges)} gauges live.")

        # transport controls only make sense for a seekable replay
        is_replay = hasattr(source, "seek")
        self.replay_ctl.setVisible(is_replay)
        if is_replay:
            dur = source.duration()
            self.pos_slider.setRange(0, int(dur * 10))
            self.btn_pause.setText("⏸ Pause")
            self.speed_combo.blockSignals(True)
            self.speed_combo.setCurrentText(f"{source.speed:g}×")
            self.speed_combo.blockSignals(False)
            self.time_label.setText(self._fmt_pos(0.0))

    # -- replay transport ---------------------------------------------------- #
    def _fmt_pos(self, pos: float) -> str:
        dur = self.source.duration() if hasattr(self.source, "duration") else 0.0
        return f"{pos:.1f} / {dur:.1f}s"

    def _toggle_pause(self):
        if not (self.source and hasattr(self.source, "pause")):
            return
        if self.source.playing:
            self.source.pause()
            self.btn_pause.setText("▶ Resume")
        else:
            self.source.resume()
            self.btn_pause.setText("⏸ Pause")

    def _on_speed(self, text: str):
        if self.source and hasattr(self.source, "set_speed"):
            try:
                self.source.set_speed(float(text.rstrip("×")))
            except ValueError:
                pass

    def _on_seek(self):
        if self.source and hasattr(self.source, "seek"):
            self.source.seek(self.pos_slider.value() / 10.0)

    def start(self):
        if not self.source:
            self.status.setText("Load a log first (Log tab → Load Log).")
            return
        try:
            # A restart can fail for real reasons (a stopped tm source reopens
            # its session file, which may have been rotated away) — a button
            # slot must report that, not raise through Qt.
            self.source.start()
        except Exception as exc:
            self.status.setText(f"Start failed: {exc}")
            return
        self.timer.start(100)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status.setText("Replaying…")

    def stop(self):
        if self.recording:
            self._finalize_record()
        self.timer.stop()
        if self.source:
            try:
                self.source.stop()
            except Exception:
                pass
        self._demote_live_badges()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status.setText("Stopped.")

    def _demote_live_badges(self):
        """A stopped source is no longer advancing: any gauge still showing a
        live-styled value must say so, not keep rendering as a live needle.
        Applies to EVERY source — a frozen GT needle after a disconnect is the
        same lie as a frozen store value."""
        if self.source is not None:
            for g in self.gauges.values():
                if (g.available and g.value is not None
                        and g.state in (None, "fresh")):
                    g.set_state("stale")

    def connect_gt(self):
        """Connect the OBDX Pro GT on a worker thread (serial IO blocks)."""
        self.status.setText("Connecting to OBDX Pro GT…")
        self.btn_connect.setEnabled(False)

        def worker():
            try:
                src = GtDataSource()
                src.start()
            except Exception as e:
                try:
                    self.gt_error.emit(str(e))
                except RuntimeError:
                    pass  # widget destroyed while connecting
                return
            try:
                self.gt_ready.emit(src)
            except RuntimeError:
                src.stop()

        threading.Thread(target=worker, name="gt-connect", daemon=True).start()

    def _on_gt_ready(self, src):
        self.bind_source(src)          # tears down the old source first
        self.timer.start(100)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_connect.setEnabled(True)
        dev = getattr(src, "device", "OBDX Pro GT")
        self._live_status = (
            f"{dev} live — {len(self.gauges)} gauges @ {getattr(src, 'port_name', '')}")
        self._source_alarm = False
        self.status.setText(self._live_status)

    def _on_gt_error(self, msg):
        self.btn_connect.setEnabled(True)
        self.status.setText(f"GT connect failed: {msg}")

    # -- truck-mcp drive-log sessions as a gauge source ---------------------- #
    def _pick_tm_session(self):
        """Menu of truck-mcp sessions, listed fresh on every click. Read-only
        store access — binding a session never touches the serial port."""
        try:
            sessions = tmstore.list_sessions()
        except OSError as exc:
            self.status.setText(f"Could not scan truck-mcp sessions: {exc}")
            return
        menu = QMenu(self)
        if not sessions:
            act = menu.addAction("No drive-log sessions found")
            act.setEnabled(False)
        for s in sessions[:25]:
            tag = "● LIVE  " if s.get("live") else ""
            label = s.get("label") or s.get("name")
            started = (s.get("started_utc") or "")[:19].replace("T", " ")
            act = menu.addAction(
                f"{tag}{label}  ·  {started}  ·  {s.get('channel_count', 0)} ch")
            if s.get("error"):
                act.setEnabled(False)
                act.setText(act.text() + "  [unreadable]")
            else:
                act.triggered.connect(
                    lambda _=False, entry=s: self._bind_tm_session(entry))
        menu.exec(self.btn_tm.mapToGlobal(self.btn_tm.rect().bottomLeft()))
        menu.deleteLater()   # parented to self — would otherwise leak per click

    def _bind_tm_session(self, entry: dict):
        path = entry["path"]
        name = entry.get("label") or entry.get("name")
        # Stop the paint timer before any teardown/modal work — _tick must not
        # keep polling a source that is mid-teardown behind a dialog. Remember
        # whether it was running: a FAILED bind leaves the old source bound and
        # must resume it, not strand a live view (or an active recording) with
        # a dead timer.
        was_active = self.timer.isActive()
        self.timer.stop()
        try:
            if entry.get("live"):
                src = tmsource.TmLiveSource(path)
                excluded = src.excluded
                self.bind_source(src)
                src.start()
                self.timer.start(100)
                self.btn_start.setEnabled(False)
                self.btn_stop.setEnabled(True)
                note = "live follow — store poll ~1 Hz"
            else:
                log, resolved = tmsource.build_replay_log(path)
                excluded = resolved.excluded
                self.bind_source(tmsource.TmReplaySource(log, speed=1.0))
                note = "archived — press ▶ Start to replay"
        except Exception as exc:
            if was_active and self.source is not None:
                self.timer.start(100)   # old source untouched — keep it live
            self.status.setText(f"Could not bind session: {exc}")
            return
        n = sum(1 for k in self.gauges if k in set(self.source.channels()))
        skip = ("  ·  excluded: " + ", ".join(
            f"{nm} ({why})" for nm, why in excluded)) if excluded else ""
        self.status.setText(
            f"🗄 {name} — {n} of {len(self.gauges)} gauges ({note}){skip}")

    def toggle_record(self):
        if not self.source:
            self.status.setText("Connect the GT (or load a log) before recording.")
            return
        if not self.recording and hasattr(self.source, "channel_states"):
            # A truck-mcp session IS already a record, with per-sample
            # freshness/quality this CSV cannot carry — re-recording it would
            # persist carried/archived values as if they were measurements,
            # decimated to the poll rate on the observer's clock.
            self.status.setText(
                "Recording is disabled for truck-mcp sessions — the drive-log "
                "store is already the record (use truck-mcp session_export).")
            return
        if not self.recording:
            avail = set(self.source.channels())
            self._rec_keys = [k for k in RECORD_ORDER if k in avail]
            try:
                self._rec_file = tempfile.NamedTemporaryFile(
                    "w", newline="", encoding="utf-8",
                    prefix="openobd_rec_", suffix=".csv", delete=False)
            except Exception as e:
                self.status.setText(f"Can't start recording: {e}")
                return
            self._rec_writer = csv.writer(self._rec_file)
            self._rec_writer.writerow(
                ["Time (s)"] + [RECORD_LABELS[k] for k in self._rec_keys])
            self._rec_count = 0
            self.source.drain()  # discard the pre-record backlog
            self.recording = True
            self.btn_record.setText("⏺ Recording… (click to save)")
            self.status.setText("Recording live data…")
        else:
            self._finalize_record()

    def _finalize_record(self):
        self.recording = False
        self.btn_record.setText("⏺ Record")
        if self.source:  # flush anything produced since the last tick
            self._record_samples(self.source.drain())
        tmp_path = self._rec_file.name
        self._rec_file.close()
        self._rec_file = self._rec_writer = None
        if not self._rec_count:
            os.unlink(tmp_path)
            self.status.setText("Nothing recorded.")
            return
        from datetime import datetime as _dt
        default = os.path.join(
            os.path.expanduser("~"), "Documents",
            "gt_log_" + _dt.now().strftime("%Y%m%d_%H%M%S") + ".csv")
        path, _f = QFileDialog.getSaveFileName(
            self, "Save datalog", default, "CSV (*.csv)")
        if not path:
            self.status.setText(
                f"Not saved — {self._rec_count} samples kept at {tmp_path}")
            return
        try:
            shutil.move(tmp_path, path)
        except Exception as e:
            self.status.setText(f"Save failed: {e} (data at {tmp_path})")
            return
        n = self._rec_count
        self.status.setText(f"Saved {n} samples → {path}")
        win = self.window()
        if hasattr(win, "load_log_path") and QMessageBox.question(
                self, "Datalog saved",
                f"Saved {n} samples to:\n{path}\n\nLoad it into Log Analysis now?"
                ) == QMessageBox.Yes:
            win.load_log_path(path)

    def _record_samples(self, samples):
        for s in samples:
            self._rec_writer.writerow([f"{s.t:.2f}"] + [
                ("" if s.values.get(k) is None else f"{s.values[k]:g}")
                for k in self._rec_keys])
            self._rec_count += 1

    def _tick(self):
        if not self.source:
            return
        s: Optional[Sample] = self.source.latest()
        # tmstore freshness vocabulary, when the source has one. Applied even
        # when latest() is None: a failed store read must flip the gauges to
        # "read failed", not freeze them on the last values as if still live.
        states = (self.source.channel_states()
                  if hasattr(self.source, "channel_states") else None)
        if states is not None:
            for key, g in self.gauges.items():
                g.set_state(states.get(key))
        # A source that can say WHY it stopped (the GT poll loop) says so in
        # the status line; when it recovers, the live caption comes back.
        msg = (self.source.status_message()
               if hasattr(self.source, "status_message") else None)
        if msg:
            self.status.setText(msg)
            self._source_alarm = True
        elif getattr(self, "_source_alarm", False):
            self._source_alarm = False
            self.status.setText(getattr(self, "_live_status", ""))
        if not s:
            if states is not None:
                for key, g in self.gauges.items():
                    if states.get(key) == "failed":
                        g.set_value(None)
            return
        for key, g in self.gauges.items():
            g.set_value(s.values.get(key))
        if self.recording:
            # drain() is lossless and duplicate-free — unlike sampling
            # latest() at the 10 Hz paint rate.
            self._record_samples(self.source.drain())
        if self.replay_ctl.isVisible() and not self.pos_slider.isSliderDown():
            pos = self.source.position()
            self.pos_slider.blockSignals(True)
            self.pos_slider.setValue(int(pos * 10))
            self.pos_slider.blockSignals(False)
            self.time_label.setText(self._fmt_pos(pos))


# --------------------------------------------------------------------------- #
# Undo command shared by every table mutation (cell edit, math, paste, revert)
# --------------------------------------------------------------------------- #
class BulkEditCommand(QUndoCommand):
    """Applies a {(r, c): (old, new)} change map to a Table. Holds the Table
    itself (not the Qt model) so undo keeps working after the user switches
    tables and back."""

    def __init__(self, win: "MainWindow", table, changes, text: str):
        super().__init__(text)
        self.win = win
        self.table = table
        self.changes = changes

    def redo(self):
        for (r, c), (_old, new) in self.changes.items():
            self.table.values[r][c] = new
        self.win._table_touched(self.table)

    def undo(self):
        for (r, c), (old, _new) in self.changes.items():
            self.table.values[r][c] = old
        self.win._table_touched(self.table)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, cal: Calibration, path: Optional[str] = None):
        super().__init__()
        self.cal = cal
        self.path = path
        self.log: Optional[Log] = None
        self.shift_events = cal.metadata.get("shift_events")
        self.current_model: Optional[CalTableModel] = None
        self.dirty = False
        self.undo_stack = QUndoStack(self)

        self.setWindowTitle(self._title())
        self.resize(1180, 760)
        self._build_menu()

        # Top level: Dashboard opens first; Tuning holds the monitor/scan/tune
        # workflow; Diagnostics holds troubleshooting/active tests/codes.
        self.settings = app_settings()
        self.dashboard = Dashboard(
            style=str(self.settings.value("dashboard/style", "classic")))
        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(self.dashboard, "Dashboard")

        tuning = QWidget()
        tl = QHBoxLayout(tuning); tl.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_tree())
        self.tune_tabs = QTabWidget()
        self.tune_tabs.addTab(self._build_editor_tab(), "Editor")
        self.tune_tabs.addTab(self._build_scalars_tab(), "Scalars")
        self.tune_tabs.addTab(self._build_log_tab(), "Log Analysis")
        splitter.addWidget(self.tune_tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 880])
        tl.addWidget(splitter)
        self.main_tabs.addTab(tuning, "Tuning")

        self.diag = DiagnosticsPage(self.dashboard)
        self.main_tabs.addTab(self.diag, "Diagnostics")

        # Live Data + Active Tests read truck-mcp's drive-log store and control
        # journal off disk (never the serial port). Appended AFTER Diagnostics
        # so the existing index-0/1/2 navigation stays valid.
        self.livedata = LiveDataPage()
        self.main_tabs.addTab(self.livedata, "Live Data")
        self.active_tests = ActiveTestsPage()
        self.main_tabs.addTab(self.active_tests, "Active Tests")

        self.main_tabs.setCurrentIndex(0)
        self.setCentralWidget(self.main_tabs)

        # select first table
        if self.cal.tables:
            self._select_table(self.cal.tables[0].name)

    # -- chrome ------------------------------------------------------------ #
    def _title(self) -> str:
        veh = self.cal.metadata.get("vehicle", "calibration")
        name = os.path.basename(self.path) if self.path else "unsaved"
        star = " *" if self.dirty else ""
        return f"OpenOBD — {veh}  [{name}]{star}"

    def _mark_dirty(self, *_a):
        if not self.dirty:
            self.dirty = True
            self.setWindowTitle(self._title())

    def closeEvent(self, event):
        if not self.dirty:
            self._shutdown_pages()
            return super().closeEvent(event)
        ans = QMessageBox.question(
            self, "Unsaved changes",
            "The calibration has unsaved edits. Save before closing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if ans == QMessageBox.Cancel:
            return event.ignore()
        if ans == QMessageBox.Save:
            self.save_cal()
            if self.dirty:  # save failed or Save As was cancelled
                return event.ignore()
        self._shutdown_pages()
        super().closeEvent(event)

    def _shutdown_pages(self):
        # A child inside a QTabWidget never gets its own closeEvent, so stop the
        # Live Data / Active Tests timers and release their DB readers explicitly.
        for page in (getattr(self, "livedata", None),
                     getattr(self, "active_tests", None)):
            if page is not None:
                page.shutdown()

    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        for label, slot, keys in [
            ("Open .cal.json…", self.open_cal, "Ctrl+O"),
            ("Save", self.save_cal, "Ctrl+S"),
            ("Save As…", self.save_cal_as, "Ctrl+Shift+S"),
            ("Export table → CSV…", self.export_csv, None),
            ("Load Log…", self.load_log, "Ctrl+L"),
        ]:
            a = QAction(label, self)
            if keys:
                a.setShortcut(keys)
            a.triggered.connect(slot)
            m.addAction(a)
        m.addSeparator()
        qa = QAction("Quit", self); qa.triggered.connect(self.close); m.addAction(qa)

        e = self.menuBar().addMenu("&Edit")
        undo = self.undo_stack.createUndoAction(self, "Undo")
        undo.setShortcut("Ctrl+Z")
        redo = self.undo_stack.createRedoAction(self, "Redo")
        redo.setShortcut("Ctrl+Y")
        e.addAction(undo); e.addAction(redo)
        e.addSeparator()
        for label, slot, keys in [
            ("Copy selection", self.copy_selection, "Ctrl+C"),
            ("Paste", self.paste_selection, "Ctrl+V"),
        ]:
            a = QAction(label, self)
            a.setShortcut(keys)
            a.triggered.connect(slot)
            e.addAction(a)
        e.addSeparator()
        rs = QAction("Revert selection → stock", self)
        rs.triggered.connect(self._revert_selected); e.addAction(rs)
        rt = QAction("Revert whole table → stock", self)
        rt.triggered.connect(self._revert_table); e.addAction(rt)

        s = self.menuBar().addMenu("&Settings")
        pref = QAction("Preferences…", self)
        pref.setShortcut("Ctrl+,")
        pref.triggered.connect(self._open_settings)
        s.addAction(pref)

        h = self.menuBar().addMenu("&Help")
        ab = QAction("About", self); ab.triggered.connect(self._about); h.addAction(ab)

    def _open_settings(self):
        """Settings dialog. Currently houses the dashboard-visual selector;
        the choice applies immediately and persists via QSettings."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        form = QFormLayout(dlg)
        combo = QComboBox()
        for key, label in DASHBOARD_STYLES.items():
            combo.addItem(label, key)
        combo.setCurrentIndex(max(0, combo.findData(self.dashboard.style)))
        form.addRow("Dashboard visual:", combo)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() != QDialog.Accepted:
            return
        style = combo.currentData()
        self.dashboard.set_style(style)
        self.settings.setValue("dashboard/style", style)
        self.statusBar().showMessage(
            f"Dashboard visual: {DASHBOARD_STYLES[style]}", 4000)

    def _about(self):
        md = self.cal.metadata
        QMessageBox.information(
            self, "About OpenOBD",
            f"OpenOBD {__version__} — calibration viewer/editor + log "
            "overlay for truck-mcp.\n\n"
            f"Vehicle: {md.get('vehicle','')}\n"
            f"Controllers: {md.get('controllers','')}\n\n"
            + md.get("safety", ""),
        )

    # -- left tree --------------------------------------------------------- #
    def _build_tree(self) -> QWidget:
        host = QWidget(); lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(2)
        self.tree_filter = QLineEdit()
        self.tree_filter.setPlaceholderText("Filter tables…")
        self.tree_filter.setClearButtonEnabled(True)
        self.tree_filter.textChanged.connect(self._filter_tree)
        lay.addWidget(self.tree_filter)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Tables"])
        cats: dict[str, QTreeWidgetItem] = {}
        for t in self.cal.tables:
            cat = t.category.split("/")[0] or "Uncategorized"
            if cat not in cats:
                node = QTreeWidgetItem([cat]); self.tree.addTopLevelItem(node)
                cats[cat] = node
            leaf = QTreeWidgetItem([t.name.replace("WOT Shift Speed — ", "")])
            leaf.setData(0, Qt.UserRole, t.name)
            cats[cat].addChild(leaf)
        self.tree.expandAll()
        self.tree.itemClicked.connect(self._on_tree_click)
        lay.addWidget(self.tree, 1)
        return host

    def _filter_tree(self, text: str):
        needle = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            cat = self.tree.topLevelItem(i)
            any_visible = False
            for j in range(cat.childCount()):
                leaf = cat.child(j)
                full = (leaf.data(0, Qt.UserRole) or "").lower()
                hit = not needle or needle in full or needle in leaf.text(0).lower()
                leaf.setHidden(not hit)
                any_visible = any_visible or hit
            cat.setHidden(not any_visible)

    def _on_tree_click(self, item, _col):
        name = item.data(0, Qt.UserRole)
        if name:
            self._select_table(name)
            self.main_tabs.setCurrentIndex(1)   # Tuning workspace
            self.tune_tabs.setCurrentIndex(0)   # Editor

    # -- editor tab -------------------------------------------------------- #
    def _build_editor_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)

        ctl = QHBoxLayout()
        self.heat_chk = QCheckBox("Heatmap"); self.heat_chk.setChecked(True)
        self.heat_chk.stateChanged.connect(self._toggle_heat)
        ctl.addWidget(self.heat_chk)

        ctl.addWidget(QLabel("Overlay:"))
        self.overlay_mode = QComboBox()
        self.overlay_mode.addItems(["None", "Log count", "Log mean value"])
        self.overlay_mode.currentIndexChanged.connect(self._apply_overlay)
        ctl.addWidget(self.overlay_mode)

        ctl.addWidget(QLabel("X ch:"))
        self.x_ch = QComboBox(); ctl.addWidget(self.x_ch)
        ctl.addWidget(QLabel("Y ch:"))
        self.y_ch = QComboBox(); ctl.addWidget(self.y_ch)
        ctl.addWidget(QLabel("Value:"))
        self.v_ch = QComboBox(); ctl.addWidget(self.v_ch)
        for cb in (self.x_ch, self.y_ch, self.v_ch):
            cb.addItem("(none)")
            cb.currentIndexChanged.connect(self._apply_overlay)

        self.next_delta_btn = QPushButton("Next Δ")
        self.next_delta_btn.setToolTip("Jump to the next changed-vs-stock cell")
        self.next_delta_btn.clicked.connect(self._goto_next_changed)
        ctl.addWidget(self.next_delta_btn)
        ctl.addStretch(1)
        lay.addLayout(ctl)

        # selection-math toolbar (HP Tuners staples). +/- keys nudge by Amount.
        math_bar = QHBoxLayout()
        math_bar.addWidget(QLabel("Amount:"))
        self.amount_edit = QLineEdit("1")
        self.amount_edit.setFixedWidth(70)
        self.amount_edit.setToolTip(
            "Operand for the math buttons and the +/- key nudge")
        math_bar.addWidget(self.amount_edit)
        for label, tip, fn in [
            ("=", "Set selection to Amount", lambda: self._math_op("set")),
            ("+", "Add Amount to selection", lambda: self._math_op("add")),
            ("−", "Subtract Amount from selection",
             lambda: self._math_op("add", negate=True)),
            ("×", "Multiply selection by Amount", lambda: self._math_op("mul")),
            ("%", "Scale selection by Amount percent",
             lambda: self._math_op("pct")),
        ]:
            b = QPushButton(label); b.setFixedWidth(34); b.setToolTip(tip)
            b.clicked.connect(fn)
            math_bar.addWidget(b)
        math_bar.addSpacing(12)
        for label, mode in [("Interp ↔", "h"), ("Interp ↕", "v"),
                            ("Interp 2D", "2d")]:
            b = QPushButton(label)
            b.setToolTip("Linear interpolation across the selection")
            b.clicked.connect(lambda _=False, m=mode: self._interp(m))
            math_bar.addWidget(b)
        math_bar.addSpacing(12)
        rb = QPushButton("Revert sel → stock")
        rb.clicked.connect(self._revert_selected)
        math_bar.addWidget(rb)
        math_bar.addStretch(1)
        lay.addLayout(math_bar)

        self.view = QTableView()
        self.view.setItemDelegate(HeatmapDelegate(self.view))
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.view.installEventFilter(self)  # +/- nudge keys
        lay.addWidget(self.view, 1)

        self.table_info = QLabel(""); self.table_info.setWordWrap(True)
        self.table_info.setStyleSheet("color:#9aa0a6; padding:4px;")
        lay.addWidget(self.table_info)
        return w

    def _select_table(self, name: str):
        t = self.cal.table(name)
        if not t:
            return
        events = self.shift_events if "Shift Speed" in name else None
        self.current_model = CalTableModel(t, shift_events=events)
        self.current_model.dataChanged.connect(self._mark_dirty)
        self.current_model.edit_hook = self._on_cell_edit
        self.current_model.show_heatmap = self.heat_chk.isChecked()
        self.view.setModel(self.current_model)
        self._update_table_info()
        self._apply_overlay()

    def _update_table_info(self):
        if not self.current_model:
            return
        t = self.current_model.table
        changed = len(editops.changed_cells(t))
        pid = f" · HPT id {t.param_id}" if t.param_id else ""
        self.table_info.setText(
            f"<b>{t.name}</b> ({t.unit}){pid} · {changed} cell(s) changed vs "
            f"stock<br>{t.note}"
        )

    def _toggle_heat(self):
        if self.current_model:
            self.current_model.set_heatmap(self.heat_chk.isChecked())

    # -- editing: undo plumbing --------------------------------------------- #
    def _on_cell_edit(self, r, c, old, new):
        self.undo_stack.push(BulkEditCommand(
            self, self.current_model.table, {(r, c): (old, new)}, "edit cell"))

    def _table_touched(self, table):
        """Called by undo commands after writing to a Table."""
        self._mark_dirty()
        if self.current_model and self.current_model.table is table:
            self.current_model.refresh_all()
            self._update_table_info()

    def _push_changes(self, changes, text):
        if not changes:
            self.statusBar().showMessage(f"{text}: no cells affected", 3000)
            return
        self.undo_stack.push(BulkEditCommand(
            self, self.current_model.table, changes, text))
        self.statusBar().showMessage(f"{text}: {len(changes)} cell(s)", 3000)

    def _selected_cells(self):
        if not self.current_model:
            return []
        sel = self.view.selectionModel()
        idxs = sel.selectedIndexes() if sel else []
        if not idxs:
            cur = self.view.currentIndex()
            idxs = [cur] if cur.isValid() else []
        return [(i.row(), i.column()) for i in idxs]

    def _amount(self) -> Optional[float]:
        try:
            return float(self.amount_edit.text())
        except ValueError:
            self.statusBar().showMessage(
                f"Amount is not a number: {self.amount_edit.text()!r}", 4000)
            return None

    def _math_op(self, op: str, negate: bool = False):
        cells = self._selected_cells()
        amt = self._amount()
        if not cells or amt is None:
            return
        if negate:
            amt = -amt
        label = {"set": "set", "add": "add", "mul": "multiply", "pct": "scale %"}[op]
        self._push_changes(
            editops.apply_math(self.current_model.table, cells, op, amt), label)

    def _interp(self, mode: str):
        cells = self._selected_cells()
        if len(cells) < 2:
            self.statusBar().showMessage("Select a range to interpolate", 3000)
            return
        self._push_changes(
            editops.interpolate(self.current_model.table, cells, mode),
            f"interpolate {mode}")

    def _revert_selected(self):
        cells = self._selected_cells()
        if not cells:
            return
        self._push_changes(
            editops.revert_cells(self.current_model.table, cells),
            "revert selection to stock")

    def _revert_table(self):
        if not self.current_model:
            return
        t = self.current_model.table
        self._push_changes(
            editops.revert_cells(t, editops.all_cells(t)),
            "revert table to stock")

    # -- clipboard ----------------------------------------------------------- #
    def copy_selection(self):
        cells = self._selected_cells()
        if not cells:
            return
        QApplication.clipboard().setText(
            editops.to_tsv(self.current_model.table, cells))
        self.statusBar().showMessage(f"Copied {len(cells)} cell(s)", 3000)

    def paste_selection(self):
        if not self.current_model:
            return
        grid = editops.parse_tsv(QApplication.clipboard().text())
        if not grid:
            self.statusBar().showMessage("Clipboard has no numeric grid", 3000)
            return
        cur = self.view.currentIndex()
        r0, c0 = (cur.row(), cur.column()) if cur.isValid() else (0, 0)
        self._push_changes(
            editops.paste_grid(self.current_model.table, r0, c0, grid), "paste")

    # -- navigation ----------------------------------------------------------- #
    def _goto_next_changed(self):
        if not self.current_model:
            return
        t = self.current_model.table
        changed = editops.changed_cells(t)
        if not changed:
            self.statusBar().showMessage("No changed cells in this table", 3000)
            return
        cur = self.view.currentIndex()
        pos = (cur.row(), cur.column()) if cur.isValid() else (-1, -1)
        nxt = next((rc for rc in changed if rc > pos), changed[0])
        idx = self.current_model.index(*nxt)
        self.view.setCurrentIndex(idx)
        self.view.scrollTo(idx)

    def eventFilter(self, obj, event):
        # +/- on the grid nudges the selection by Amount (HP Tuners style).
        if obj is self.view and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Plus, Qt.Key_Minus) and self.current_model:
                self._math_op("add", negate=event.key() == Qt.Key_Minus)
                return True
        return super().eventFilter(obj, event)

    def _refresh_channel_combos(self):
        keys = ["(none)"] + (self.log.canonical_keys() if self.log else [])
        for cb in (self.x_ch, self.y_ch, self.v_ch):
            cur = cb.currentText()
            cb.blockSignals(True)
            cb.clear(); cb.addItems(sorted(set(keys), key=keys.index))
            i = cb.findText(cur)
            cb.setCurrentIndex(i if i >= 0 else 0)
            cb.blockSignals(False)
        # sensible defaults for a shift table: x=vss
        if self.log and self.x_ch.currentText() == "(none)":
            i = self.x_ch.findText("vss")
            if i >= 0:
                self.x_ch.setCurrentIndex(i)

    def _apply_overlay(self):
        if not self.current_model:
            return
        mode = self.overlay_mode.currentIndex()
        if mode == 0 or not self.log:
            self.current_model.set_overlay(None, CalTableModel.OVERLAY_NONE)
            return
        xk = self.x_ch.currentText()
        if xk == "(none)":
            self.current_model.set_overlay(None, CalTableModel.OVERLAY_NONE)
            return
        yk = self.y_ch.currentText()
        vk = self.v_ch.currentText()
        yk = None if yk == "(none)" else yk
        vk = None if vk == "(none)" else vk
        try:
            ov = bin_log_to_table(
                self.log, self.current_model.table,
                x_channel=xk, y_channel=yk, value_channel=vk,
            )
        except Exception as e:
            # Clear rather than leave a stale overlay on screen, and say why.
            self.current_model.set_overlay(None, CalTableModel.OVERLAY_NONE)
            self.statusBar().showMessage(f"Overlay failed: {e}", 6000)
            return
        m = (CalTableModel.OVERLAY_COUNT if mode == 1
             else CalTableModel.OVERLAY_MEAN)
        self.current_model.set_overlay(ov, m)

    # -- scalars tab ------------------------------------------------------- #
    def _build_scalars_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        self.scalar_tbl = QTableWidget(len(self.cal.scalars), 6)
        self.scalar_tbl.setHorizontalHeaderLabels(
            ["Parameter", "Value", "Unit", "Stock", "Δ", "Category"])
        self.scalar_tbl.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        for r, s in enumerate(self.cal.scalars):
            self._set_scalar_row(r, s)
        self.scalar_tbl.itemChanged.connect(self._on_scalar_edit)
        lay.addWidget(self.scalar_tbl)
        note = QLabel(self.cal.metadata.get("safety", ""))
        note.setWordWrap(True); note.setStyleSheet("color:#e07b72; padding:6px;")
        lay.addWidget(note)
        return w

    def _set_scalar_row(self, r, s):
        def item(text, editable=False, color=None):
            it = QTableWidgetItem(text)
            if not editable:
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            if color:
                it.setForeground(color)
            return it
        self.scalar_tbl.blockSignals(True)
        self.scalar_tbl.setItem(r, 0, item(s.name))
        vi = item(f"{s.value:g}", editable=True)
        vi.setData(Qt.UserRole, r)
        self.scalar_tbl.setItem(r, 1, vi)
        self.scalar_tbl.setItem(r, 2, item(s.unit))
        self.scalar_tbl.setItem(
            r, 3, item("" if s.stock_value is None else f"{s.stock_value:g}"))
        delta = ""
        if s.stock_value is not None:
            d = s.value - s.stock_value
            delta = f"{d:+g}" if d else ""
        self.scalar_tbl.setItem(
            r, 4, item(delta, color=QColor(235, 110, 100) if delta else None))
        self.scalar_tbl.setItem(r, 5, item(s.category))
        self.scalar_tbl.blockSignals(False)

    def _on_scalar_edit(self, it):
        if it.column() != 1:
            return
        r = it.data(Qt.UserRole)
        if r is None:
            r = it.row()
        text = it.text()
        try:
            v = float(text)
        except ValueError:
            # Restore the display so the cell never shows a value the model
            # doesn't hold. _set_scalar_row replaces the row's items, deleting
            # `it` — so only the pre-captured text may be used after it.
            self._set_scalar_row(r, self.cal.scalars[r])
            self.statusBar().showMessage(
                f"Not a number: {text!r} — edit reverted", 4000)
            return
        if v != self.cal.scalars[r].value:
            self._mark_dirty()
        self.cal.scalars[r].value = v
        self._set_scalar_row(r, self.cal.scalars[r])

    # -- log tab ----------------------------------------------------------- #
    def _build_log_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        b = QPushButton("Load Log…"); b.clicked.connect(self.load_log)
        bar.addWidget(b)
        self.events_chk = QCheckBox("Event markers")
        self.events_chk.setChecked(True)
        self.events_chk.setToolTip("Knock events (red) and shifts (cyan)")
        self.events_chk.toggled.connect(self._rebuild_plots)
        bar.addWidget(self.events_chk)
        self.cursor_label = QLabel("")
        self.cursor_label.setStyleSheet("color:#9aa0a6;")
        bar.addWidget(self.cursor_label, 1)
        lay.addLayout(bar)

        # chart state
        self._log_times: list[float] = []
        self._plots: list = []
        self._vlines: list = []
        self._knock_times: list[float] = []
        self._shift_times: list[float] = []

        split = QSplitter(Qt.Vertical)
        top = QSplitter(Qt.Horizontal)
        self.chan_list = QListWidget()
        self.chan_list.setMaximumWidth(180)
        self.chan_list.itemChanged.connect(self._rebuild_plots)
        top.addWidget(self.chan_list)
        self.plot_area = pg.GraphicsLayoutWidget()
        self.plot_area.scene().sigMouseMoved.connect(self._on_chart_mouse)
        top.addWidget(self.plot_area)
        top.setStretchFactor(0, 0); top.setStretchFactor(1, 1)
        split.addWidget(top)

        self.log_report = QTextEdit(); self.log_report.setReadOnly(True)
        self.log_report.setFont(QFont("Consolas", 10))
        self.log_report.setPlainText(
            "No log loaded.\n\nLoad a VCM Scanner CSV export (Log File → Export "
            "Log File → CSV) or a plain CSV. Channels plot above with a synced "
            "cursor; this report shows regime trims, knock events, and "
            "fuel-pressure flags; the Editor tab can then overlay the log onto "
            "any table.")
        split.addWidget(self.log_report)
        split.setSizes([460, 220])
        lay.addWidget(split, 1)
        return w

    # -- charting ------------------------------------------------------------ #
    _PLOT_DEFAULTS = ["rpm", "vss", "app", "tps", "spark", "knock_retard"]

    def _populate_channels(self):
        self.chan_list.blockSignals(True)
        self.chan_list.clear()
        keys = [k for k in self.log.canonical_keys() if k != "time"]
        present = set(keys)
        # default to the tuning staples that are actually in the log (max 4)
        want = [k for k in self._PLOT_DEFAULTS if k in present][:4] or keys[:3]
        for k in keys:
            it = QListWidgetItem(k)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if k in want else Qt.Unchecked)
            self.chan_list.addItem(it)
        self.chan_list.blockSignals(False)

    def _checked_channels(self) -> list[str]:
        return [self.chan_list.item(i).text()
                for i in range(self.chan_list.count())
                if self.chan_list.item(i).checkState() == Qt.Checked]

    def _rebuild_plots(self, *_a):
        self.plot_area.clear()
        self._plots = []; self._vlines = []
        if not self.log or not self._log_times:
            return
        times = np.asarray(self._log_times, dtype=float)
        keys = self._checked_channels()
        first = None
        for i, key in enumerate(keys):
            p = self.plot_area.addPlot(row=i, col=0)
            ser = self.log.series(key)
            y = np.array([np.nan if v is None else v for v in ser], dtype=float)
            n = min(len(times), len(y))
            p.plot(times[:n], y[:n],
                   pen=pg.mkPen((120, 170, 240), width=1.4), connect="finite")
            p.setLabel("left", key)
            p.showGrid(x=True, y=True, alpha=0.15)
            if first is None:
                first = p
            else:
                p.setXLink(first)
            if i < len(keys) - 1:
                p.getAxis("bottom").setStyle(showValues=False)
            if self.events_chk.isChecked():
                for t_ev in self._knock_times:
                    p.addItem(pg.InfiniteLine(
                        pos=t_ev, angle=90,
                        pen=pg.mkPen((220, 70, 60, 150), width=1,
                                     style=Qt.DashLine)), ignoreBounds=True)
                for t_ev in self._shift_times:
                    p.addItem(pg.InfiniteLine(
                        pos=t_ev, angle=90,
                        pen=pg.mkPen((80, 200, 220, 130), width=1,
                                     style=Qt.DashLine)), ignoreBounds=True)
            vline = pg.InfiniteLine(
                angle=90, movable=False,
                pen=pg.mkPen((235, 235, 235, 120), width=1))
            vline.setVisible(False)
            p.addItem(vline, ignoreBounds=True)
            self._vlines.append(vline)
            self._plots.append(p)

    def _on_chart_mouse(self, scene_pos):
        if not self._plots or not self.log or not self._log_times:
            return
        x = self._plots[0].vb.mapSceneToView(scene_pos).x()
        import bisect
        i = bisect.bisect_right(self._log_times, x) - 1
        if i < 0 or i >= self.log.n_samples:
            for vl in self._vlines:
                vl.setVisible(False)
            self.cursor_label.setText("")
            return
        for vl in self._vlines:
            vl.setPos(x); vl.setVisible(True)
        parts = [f"t={self._log_times[i]:.2f}s"]
        for key in self._checked_channels():
            ser = self.log.series(key)
            v = ser[i] if i < len(ser) else None
            parts.append(f"{key}={v:g}" if v is not None else f"{key}=—")
        self.cursor_label.setText("   ".join(parts))

    def _render_report(self, rep=None):
        if not self.log:
            return
        rep = rep or analyze_log(self.log)
        L = []
        L.append(f"Source: {self.log.source}")
        L.append(f"Samples: {rep.n_samples}"
                 + (f"  ·  Duration: {rep.duration_s:.1f}s"
                    if rep.duration_s else ""))
        L.append(f"Channels: {', '.join(rep.channels_present)}")
        L.append("")
        L.append("── Regime trims ──")
        for rg in rep.regimes:
            s = f"  {rg.name:22s} n={rg.n:<6d}"
            if rg.combined_trim_mean is not None:
                s += f" trim {rg.combined_trim_mean:+.1f}%"
                if rg.stft_mean is not None:
                    s += f"  (STFT {rg.stft_mean:+.1f}, LTFT {rg.ltft_mean or 0:+.1f})"
            L.append(s)
        L.append("")
        L.append("── Knock ──")
        if rep.max_knock_retard is not None:
            L.append(f"  max KR: {rep.max_knock_retard:.1f}°  ·  "
                     f"{len(rep.knock_events)} event(s)")
        for e in rep.knock_events[:12]:
            if e.get("inferred"):
                L.append(f"    ~t={e.get('time_s')}  spark drop "
                         f"{e.get('spark_drop_deg')}°  @ {e.get('rpm')} rpm (inferred)")
            else:
                L.append(f"    t={e.get('time_s')}  KR {e.get('retard_deg')}°  "
                         f"@ {e.get('rpm')} rpm")
        if rep.max_knock_retard is None and not rep.knock_events:
            L.append("  none / not logged")
        L.append("")
        if rep.fuel_pressure_min is not None:
            L.append(f"── Fuel pressure ── min {rep.fuel_pressure_min:.0f}"
                     + ("  ⚠ SAG" if rep.fuel_pressure_sag else ""))
            L.append("")
        # Observed shift points — the meaningful comparison for the shift tables.
        shifts = detect_shift_points(self.log)
        wot_shifts = [s for s in shifts if s.wot]
        L.append("── Observed shift points ──")
        if not shifts:
            L.append("  none detected (need a 'gear' channel or a clear RPM drop)")
        else:
            src = "gear channel" if not shifts[0].inferred else "inferred (RPM drop)"
            L.append(f"  {len(shifts)} shift(s) via {src}; "
                     f"{len(wot_shifts)} at WOT")
            for s in (wot_shifts or shifts)[:12]:
                gear = (f"{s.from_gear}->{s.to_gear}"
                        if s.from_gear is not None else "up")
                rpm = f"{s.rpm:.0f} rpm" if s.rpm is not None else "?"
                vss = f"{s.vss:.0f} mph" if s.vss is not None else "?"
                flag = " ⚑WOT" if s.wot else ""
                L.append(f"    {gear:6s} @ {rpm:>10s}  {vss:>7s}{flag}")
            L.append("  (compare WOT shifts to the setpoint tables: target "
                     "1-2/2-3 ~5,400-5,500 RPM on 4.11/35s)")
        L.append("")
        if rep.notes:
            L.append("── Notes ──")
            for n in rep.notes:
                L.append(f"  • {n}")
        self.log_report.setPlainText("\n".join(L))

    # -- file ops ---------------------------------------------------------- #
    def open_cal(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Open calibration", "", "Calibration (*.cal.json *.json)")
        if not p:
            return
        try:
            self.cal = Calibration.load(p)
        except Exception as e:
            QMessageBox.critical(self, "Open failed", str(e)); return
        self.path = p
        # rebuild UI by re-instantiating (simplest, robust). Keep a Python
        # reference on the QApplication or the new window gets GC'd.
        new = MainWindow(self.cal, p)
        QApplication.instance()._openobd_main = new
        new.show()
        self.close()

    def save_cal(self):
        if not self.path:
            return self.save_cal_as()
        try:
            self.cal.save(self.path)
            self.dirty = False
            self.setWindowTitle(self._title())
            self.statusBar().showMessage(f"Saved {self.path}", 4000)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def save_cal_as(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "Save calibration", self.path or "edited.cal.json",
            "Calibration (*.cal.json *.json)")
        if not p:
            return
        self.path = p
        self.save_cal()

    def export_csv(self):
        if not self.current_model:
            return
        t = self.current_model.table
        p, _ = QFileDialog.getSaveFileName(
            self, "Export table CSV", f"{t.name}.csv", "CSV (*.csv)")
        if not p:
            return
        import csv
        with open(p, "w", newline="", encoding="utf-8") as fh:
            wtr = csv.writer(fh)
            hdr = [""] + (
                self.shift_events if ("Shift" in t.name and self.shift_events)
                else [f"{v:g}" for v in t.x_axis.values])
            wtr.writerow(hdr)
            for r in range(t.n_rows):
                ylab = (f"{t.y_axis.values[r]:g}" if t.y_axis else t.unit)
                wtr.writerow([ylab] + [f"{v:g}" for v in t.values[r]])
        self.statusBar().showMessage(f"Exported {p}", 4000)

    def load_log(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Load log", "", "Logs (*.csv);;All files (*)")
        if p:
            self.load_log_path(p)

    def load_log_path(self, p):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            self.log = parse_csv(text, source=os.path.basename(p))
        except Exception as e:
            QMessageBox.critical(self, "Load log failed", str(e)); return
        self._log_times = time_axis(self.log)
        rep = analyze_log(self.log)
        # Event marker positions. The chart axis is zero-based (time_axis
        # subtracts the log's first timestamp); analyzer events carry RAW
        # timestamps, so index into _log_times (knock has a sample index)
        # or subtract the base (shifts only carry time_s).
        raw_t = [v for v in self.log.series("time") if v is not None]
        t_base = raw_t[0] if raw_t else 0.0
        self._knock_times = []
        for e in rep.knock_events:
            i = e.get("sample")
            if i is not None and i < len(self._log_times):
                self._knock_times.append(self._log_times[i])
            elif e.get("time_s") is not None:
                self._knock_times.append(float(e["time_s"]) - t_base)
        self._shift_times = [float(s.time_s) - t_base
                             for s in detect_shift_points(self.log)
                             if s.time_s is not None]
        self._render_report(rep)
        self._populate_channels()
        self._rebuild_plots()
        self._refresh_channel_combos()
        self.dashboard.bind_source(LogReplaySource(self.log, speed=4.0))
        self.main_tabs.setCurrentIndex(1)   # Tuning workspace
        self.tune_tabs.setCurrentIndex(2)   # Log Analysis
        self.statusBar().showMessage(
            f"Loaded log: {self.log.n_samples} samples, "
            f"{len(self.log.canonical_keys())} mapped channels", 6000)


def load_default_cal() -> tuple[Calibration, Optional[str]]:
    # Candidate locations: dev tree, and PyInstaller's bundled data dir.
    candidates = []
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(here, "data", "2010_silverado_full.cal.json"))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "data",
                                       "2010_silverado_full.cal.json"))
    for seed_path in candidates:
        if os.path.exists(seed_path):
            # A frozen bundle's seed is read-only; return no path so Save prompts
            # the user for a writable location instead of failing on the bundle.
            writable_path = None if meipass and seed_path.startswith(meipass) \
                else seed_path
            return Calibration.load(seed_path), writable_path
    from . import seed_2010_silverado as s
    return s.build_with_labels(), None


def apply_dark_theme(app: QApplication) -> None:
    """App-wide dark Fusion palette (dark mode is the primary theme)."""
    app.setStyle("Fusion")
    p = QPalette()
    window = QColor(37, 39, 43)
    base = QColor(28, 30, 34)
    text = QColor(222, 224, 228)
    disabled = QColor(120, 124, 130)
    highlight = QColor(53, 106, 195)
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, window)
    p.setColor(QPalette.ToolTipBase, QColor(48, 50, 56))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.PlaceholderText, disabled)
    p.setColor(QPalette.Button, QColor(45, 48, 53))
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor(255, 120, 110))
    p.setColor(QPalette.Link, QColor(120, 170, 240))
    p.setColor(QPalette.Highlight, highlight)
    p.setColor(QPalette.HighlightedText, QColor(245, 246, 248))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, disabled)
    app.setPalette(p)


def app_icon_path() -> Optional[str]:
    # Candidate locations: dev tree, and PyInstaller's bundled data dir.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [os.path.join(here, "assets", "openobd.ico")]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "assets", "openobd.ico"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    app = QApplication(argv)
    icon_path = app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    apply_dark_theme(app)
    args = argv[1:]
    want_gt = "--gt" in args
    paths = [a for a in args if not a.startswith("-") and os.path.exists(a)]
    if paths:
        cal, path = Calibration.load(paths[0]), paths[0]
    else:
        cal, path = load_default_cal()
    win = MainWindow(cal, path)
    win.show()
    if want_gt:
        try:
            win.main_tabs.setCurrentIndex(0)   # Dashboard
        except Exception:
            pass
        QTimer.singleShot(300, win.dashboard.connect_gt)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
