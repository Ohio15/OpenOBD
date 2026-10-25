"""
gauges — the Dashboard's gauge widgets, cluster layouts and frame animator.

Moved out of app.py so the gauge rendering can evolve without touching the
Dashboard's data plumbing. What makes the cluster fluent:

  * Needles, arcs and bar fills ease toward the latest real value at display
    rate (one shared ~60 fps QTimer, running only while something is moving),
    decoupled from the 1-10 Hz sample rate. The readout text always shows the
    true latest sample (gaugemotion.NeedleMotion — easing is visual only).
  * The expensive, value-independent layer of each dial/bar (face, ticks,
    numerals, zones, scale) is rendered once into a cached pixmap and reused
    every frame; a gauge repaints only when its value, state or eased
    position actually changed.
  * Bar/ring strips flow into a balanced grid that reflows on resize, and
    gauges for channels the bound source can't feed collapse out of the way.
  * Every gauge shows its freshness in the same slot: a green "live" mark
    when fresh, the tmstore badge caption otherwise.
"""
from __future__ import annotations

import math
import time
from typing import Optional

from PySide6.QtCore import QObject, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPalette, QPen,
    QPixmap, QPolygonF, QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLayout, QSizePolicy, QVBoxLayout, QWidget,
)

from .gaugemotion import (
    NeedleMotion, fmt_reading, grid_cells, grid_shape, reading_decimals,
)

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

#: Same green Live Data uses for "fresh" (livedata.STATE_STYLE).
LIVE_COLOR = QColor(0x5F, 0xD0, 0x8A)


def _nice_step(span: float, target: int = 8) -> float:
    """A 1/2/2.5/5×10^k step giving roughly `target` major ticks."""
    raw = span / max(1, target)
    mag = 10.0 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for m in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= m * mag:
            return m * mag
    return 10.0 * mag


def _font(size: float, bold: bool = False, spacing: float = 0.0) -> QFont:
    f = QFont()
    f.setPointSizeF(size)
    f.setBold(bold)
    if spacing:
        f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    return f


# --------------------------------------------------------------------------- #
# Frame animator: one shared display-rate timer for every moving gauge
# --------------------------------------------------------------------------- #
class _Animator(QObject):
    """Drives NeedleMotion for the gauges that are mid-motion. The timer runs
    only while at least one visible gauge is moving, so an idle or hidden
    cluster costs nothing. Frame times come from the monotonic clock, so timer
    jitter changes smoothness, never speed."""

    FRAME_MS = 16

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._moving: set = set()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._frame)

    def kick(self, gauge: "GaugeBase") -> None:
        if not gauge.isVisible():
            # nobody can see the motion — land on the value, owe no frames
            gauge.motion.snap()
            return
        self._moving.add(gauge)
        if not self._timer.isActive():
            self._timer.start()

    def _frame(self) -> None:
        now = time.monotonic()
        for g in list(self._moving):
            try:
                if not g.isVisible():
                    g.motion.snap()
                    self._moving.discard(g)
                    continue
                if g.motion.step(now):
                    g.update()
                if not g.motion.moving:
                    self._moving.discard(g)
            except RuntimeError:
                # C++ side deleted (cluster rebuilt by a style switch)
                self._moving.discard(g)
        if not self._moving:
            self._timer.stop()


_ANIMATOR: Optional[_Animator] = None


def _animator() -> Optional[_Animator]:
    global _ANIMATOR
    if _ANIMATOR is None:
        app = QApplication.instance()
        if app is None:
            return None
        _ANIMATOR = _Animator(app)
    return _ANIMATOR


# --------------------------------------------------------------------------- #
# HP-Tuners-style gauges: round dials with needles and vertical bars. Pure
# QPainter, no deps.
# --------------------------------------------------------------------------- #
class GaugeBase(QWidget):
    """Shared value/min-max/availability/freshness/motion plumbing.

    `value` is the true latest sample (what the readout prints); `shown` is
    the eased geometry position (where the needle/arc/fill is drawn)."""

    def __init__(self, key, label, unit, vmin, vmax, collapsible=True):
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
        # Collapsible gauges hide when the bound source can't feed them;
        # anchors (the big RPM/Speed dials) stay put, dimmed.
        self.collapsible = collapsible
        self.motion = NeedleMotion(self.vmin, self.vmax)
        self.decimals = reading_decimals(self.vmin, self.vmax, key)
        self._static: Optional[QPixmap] = None
        self._static_key = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setToolTip(f"{label} — click to reset min/max capture")

    # -- data ------------------------------------------------------------- #
    @property
    def shown(self) -> float:
        return self.motion.shown

    def reset(self):
        self.value = None
        self.min_seen = self.max_seen = None
        self.state = None
        self.motion.reset()
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

    def _is_live(self) -> bool:
        """Positive confirmation: fresh state AND a value on screen."""
        return (self.available and self.state == "fresh"
                and self.value is not None)

    def set_available(self, on: bool):
        if on != self.available:
            self.available = on
            self._static_key = None        # dimmed face is a different layer
            self.update()
        if self.collapsible:
            if not on:
                self.hide()
            elif self.parentWidget() is not None:
                # never show() an orphan: it would pop up as its own window
                self.show()

    def set_value(self, v: Optional[float]):
        if v is not None and not math.isfinite(v):
            v = None                       # NaN/inf is not a reading
        changed = v != self.value
        self.value = v
        if v is not None:
            if self.min_seen is None or v < self.min_seen:
                self.min_seen = v
                changed = True
            if self.max_seen is None or v > self.max_seen:
                self.max_seen = v
                changed = True
        if self.motion.set_target(v):
            anim = _animator()
            if anim is not None:
                anim.kick(self)
            else:
                self.motion.snap()
            changed = True
        if changed:
            self.update()

    def mousePressEvent(self, _event):
        self.min_seen = self.max_seen = None
        self.update()

    def _frac(self, v: float) -> float:
        span = (self.vmax - self.vmin) or 1.0
        return max(0.0, min(1.0, (v - self.vmin) / span))

    def _fmt(self, v: Optional[float]) -> str:
        return fmt_reading(v, self.decimals)

    # -- cached static layer --------------------------------------------- #
    def _paint_static(self, p: QPainter, w: int, h: int) -> None:
        """Value-independent drawing (face, ticks, scale). Subclasses that
        override this get it cached; the default draws nothing."""

    def _draw_static(self, p: QPainter, w: int, h: int) -> None:
        dpr = self.devicePixelRatioF()
        key = (w, h, dpr, self.available)
        if key != self._static_key:
            pm = QPixmap(max(1, int(w * dpr)), max(1, int(h * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.transparent)
            sp = QPainter(pm)
            sp.setRenderHint(QPainter.Antialiasing)
            self._paint_static(sp, w, h)
            sp.end()
            self._static, self._static_key = pm, key
        p.drawPixmap(0, 0, self._static)

    def _live_dot(self, p: QPainter, x: float, y: float, r: float = 3.0):
        p.setPen(Qt.NoPen)
        p.setBrush(LIVE_COLOR)
        p.drawEllipse(QPointF(x, y), r, r)


class DialGauge(GaugeBase):
    """Round analog dial: radial blue face, tick ring, yellow/red zones, a
    needle (parked at min until data arrives), digital readout."""

    SWEEP_START = 225.0   # degrees, math orientation (0=east, CCW+)
    SWEEP = 270.0         # clockwise sweep from SWEEP_START

    def __init__(self, key, label, unit, vmin, vmax, large=False,
                 divisor=1.0, yellow_from=None, red_from=None,
                 collapsible=True):
        super().__init__(key, label, unit, vmin, vmax, collapsible=collapsible)
        self.divisor = float(divisor)
        self.yellow_from = yellow_from
        self.red_from = red_from
        side = 200 if large else 120
        self.setMinimumSize(side, side)
        self._fonts_side = None

    def _angle(self, v: float) -> float:
        return self.SWEEP_START - self.SWEEP * self._frac(v)

    def _pt(self, cx, cy, r, ang_deg):
        a = math.radians(ang_deg)
        return QPointF(cx + r * math.cos(a), cy - r * math.sin(a))

    def _geom(self, w, h):
        side = min(w, h)
        return side, w / 2.0, h / 2.0, side / 2.0 - 3

    def _paint_static(self, p, w, h):
        side, cx, cy, r = self._geom(w, h)
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
        p.setFont(_font(max(6.0, side * 0.052), bold=True))
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
                lp = self._pt(cx, cy, r * 0.68, ang)
                rect = QRectF(lp.x() - side * 0.09, lp.y() - side * 0.05,
                              side * 0.18, side * 0.1)
                p.setPen(tick_pen_c)
                p.drawText(rect, Qt.AlignCenter, f"{v / self.divisor:g}")
            v += minor

        # unit + name
        p.setPen(QColor(120, 126, 136) if dim else QColor(200, 208, 220))
        p.setFont(_font(max(6.0, side * 0.055)))
        p.drawText(QRectF(cx - r, cy - r * 0.52, 2 * r, side * 0.12),
                   Qt.AlignCenter, self.unit)
        p.drawText(QRectF(cx - r, cy + r * 0.62, 2 * r, side * 0.13),
                   Qt.AlignCenter, self.label)

    def _dyn_fonts(self, side):
        if self._fonts_side != side:
            self._f_read = _font(max(7.0, side * 0.07), bold=True)
            self._f_mm = _font(max(5.5, side * 0.042))
            self._f_badge = _font(max(5.5, side * 0.045), bold=True)
            self._fonts_side = side
        return self._f_read, self._f_mm, self._f_badge

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        side, cx, cy, r = self._geom(w, h)
        dim = not self.available
        self._draw_static(p, w, h)
        f_read, f_mm, f_badge = self._dyn_fonts(side)

        # digital readout (the true latest sample) + min/max
        p.setPen(QColor(130, 136, 146) if dim else QColor(245, 246, 248))
        p.setFont(f_read)
        p.drawText(QRectF(cx - r, cy + r * 0.30, 2 * r, side * 0.16),
                   Qt.AlignCenter, self._fmt(self.value))
        if self.max_seen is not None:
            p.setPen(QColor(150, 155, 162))
            p.setFont(f_mm)
            p.drawText(QRectF(cx - r, cy + r * 0.47, 2 * r, side * 0.1),
                       Qt.AlignCenter,
                       f"▼{self._fmt(self.min_seen)} ▲{self._fmt(self.max_seen)}")

        # freshness slot: green "live" when fresh, the badge caption otherwise —
        # a carried/stale/archived value must announce itself
        badge = self._state_badge()
        slot = QRectF(cx - r, cy - r * 0.78, 2 * r, side * 0.11)
        if badge is not None:
            p.setPen(badge[1])
            p.setFont(f_badge)
            p.drawText(slot, Qt.AlignCenter, badge[0])
        elif self._is_live():
            p.setPen(LIVE_COLOR)
            p.setFont(f_badge)
            p.drawText(slot, Qt.AlignCenter, "● live")

        # needle at the eased position (parked at vmin when no data yet)
        ang = self._angle(self.shown)
        needle = QPolygonF([self._pt(cx, cy, r * 0.88, ang),
                            self._pt(cx, cy, r * 0.07, ang + 100),
                            self._pt(cx, cy, r * 0.16, ang + 180),
                            self._pt(cx, cy, r * 0.07, ang - 100)])
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
        self.setMinimumSize(70, 150)
        self._f_label = _font(8.5, bold=True)
        self._f_value = _font(9.5, bold=True)
        self._f_unit = _font(6.5)

    def _bar_geom(self, w, h):
        top, bot = 20, h - 30
        bar_w = max(18, int(w * 0.34))
        return top, bot, 6, bar_w

    def _paint_static(self, p, w, h):
        dim = not self.available
        top, bot, bx, bar_w = self._bar_geom(w, h)

        # name (top)
        p.setPen(QColor(120, 126, 136) if dim else QColor(225, 228, 232))
        p.setFont(self._f_label)
        p.drawText(QRectF(0, 2, w, 14), Qt.AlignCenter, self.label)

        # bar frame
        p.setPen(QPen(QColor(150, 155, 164) if not dim else QColor(70, 75, 84), 1.4))
        p.setBrush(QColor(12, 14, 20))
        p.drawRect(QRectF(bx, top, bar_w, bot - top))

        # side scale: ticks, labels, red zones
        sx = bx + bar_w + 3
        step = _nice_step(self.vmax - self.vmin, target=5)
        tick_c = QColor(110, 116, 126) if dim else QColor(225, 165, 60)
        p.setFont(self._f_unit)

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

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        dim = not self.available
        self._draw_static(p, w, h)
        top, bot, bx, bar_w = self._bar_geom(w, h)
        badge = self._state_badge()

        # fill at the eased position (accented when not a live measurement)
        if self.value is not None and not dim:
            fill_top = bot - (bot - top) * self._frac(self.shown)
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

        # value + unit (bottom); takes the freshness accent so a carried/stale
        # reading can't pass for live even at bar-gauge size
        p.setPen(QColor(120, 126, 136) if dim
                 else (badge[1] if badge else QColor(240, 242, 246)))
        p.setFont(self._f_value)
        txt = self._fmt(self.value)
        if self.unit and self.value is not None:
            txt += f" {self.unit}"
        p.drawText(QRectF(0, h - 27, w, 14), Qt.AlignCenter, txt)
        p.setFont(self._f_unit)
        if badge is not None and not dim:
            p.setPen(badge[1])
            p.drawText(QRectF(0, h - 13, w, 12), Qt.AlignCenter, badge[0])
        elif self._is_live():
            self._live_dot(p, w - 6.0, 6.0)
        p.end()


# --------------------------------------------------------------------------- #
# "Modern cluster" gauges (Settings → Preferences → Dashboard visual): a dark
# dual-dial cluster with digital readout tiles. Same GaugeBase plumbing, so
# availability, freshness badges and eased motion behave identically.
# --------------------------------------------------------------------------- #
_M_TILE = QColor(16, 20, 27)
_M_TILE_DIM = QColor(12, 14, 18)
_M_EDGE = QColor(46, 54, 68)
_M_EDGE_DIM = QColor(30, 34, 42)
_M_TEXT = QColor(235, 239, 245)
_M_DIM = QColor(118, 127, 140)
_M_BLUE = QColor(58, 140, 235)
_M_RED = QColor(222, 58, 48)
_M_DIM_TEXT = QColor(120, 126, 136)


def _m_fit(font: QFont, text: str, avail_w: float) -> QFont:
    """Shrink `font` (never grow it) until `text` fits in avail_w pixels."""
    wpx = QFontMetricsF(font).horizontalAdvance(text)
    if wpx > avail_w > 0:
        font = QFont(font)
        font.setPointSizeF(max(6.0, font.pointSizeF() * avail_w / wpx))
    return font


class ModernTile(GaugeBase):
    """Rounded readout tile: small caps label, large digital value + unit."""

    def __init__(self, key, label, unit, vmin=0.0, vmax=1.0, centered=False):
        super().__init__(key, label, unit, vmin, vmax)
        self.centered = centered
        self.setMinimumSize(96, 56)
        self._f_label = _font(7.5, bold=True, spacing=1.0)
        self._f_badge = _font(6.5)

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
        p.setFont(self._f_label)
        p.drawText(QRectF(10, 5, w - 20, 13), align | Qt.AlignVCenter, self.label)

        badge = self._state_badge()
        p.setPen(_M_DIM_TEXT if dim else (badge[1] if badge else _M_TEXT))
        f2 = _font(max(11.0, min(h * 0.28, 26.0)), bold=True)
        txt = self._fmt(self.value) + (f" {self.unit}" if self.unit else "")
        p.setFont(_m_fit(f2, txt, w - 20))
        p.drawText(QRectF(10, 16, w - 20, h - (32 if badge else 20)),
                   align | Qt.AlignVCenter, txt)
        if badge is not None:
            p.setPen(badge[1])
            p.setFont(self._f_badge)
            p.drawText(QRectF(10, h - 16, w - 20, 12),
                       align | Qt.AlignVCenter, badge[0])
        elif self._is_live():
            self._live_dot(p, w - 10.0, 11.0)
        p.end()


class ModernBarTile(GaugeBase):
    """Tile with a digital readout and a horizontal blue bar + red zone."""

    def __init__(self, key, label, unit, vmin, vmax, red_above=None):
        super().__init__(key, label, unit, vmin, vmax)
        self.red_above = red_above
        self.setMinimumSize(120, 58)
        self._f_label = _font(7.5, bold=True, spacing=1.0)
        self._f_small = _font(6.0)

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
        p.setFont(self._f_label)
        p.drawText(QRectF(10, 4, w - 20, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   self.label)
        p.setPen(_M_DIM_TEXT if dim else (badge[1] if badge else _M_TEXT))
        f2 = _font(max(10.0, min(h * 0.22, 15.0)), bold=True)
        txt = self._fmt(self.value) + (f" {self.unit}" if self.unit else "")
        p.setFont(_m_fit(f2, txt, max(40.0, w - 90.0)))
        p.drawText(QRectF(10, 2, w - 20, 24), Qt.AlignRight | Qt.AlignVCenter,
                   txt)

        # middle band: freshness (left) + scale end labels
        p.setFont(self._f_small)
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
        if badge is None and self._is_live():
            self._live_dot(p, w / 2.0, h - 20.5)

        # bar: dark track, red zone, fill at the eased position
        by, bx0, bx1 = h - 14.0, 10.0, w - 10.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(28, 33, 42))
        p.drawRoundedRect(QRectF(bx0, by, bx1 - bx0, 6), 3, 3)
        if self.red_above is not None and not dim:
            x = bx0 + (bx1 - bx0) * self._frac(self.red_above)
            p.setBrush(QColor(120, 30, 26))
            p.drawRoundedRect(QRectF(x, by, bx1 - x, 6), 3, 3)
        if self.value is not None and not dim:
            # zone colour follows the true reading, not the eased position
            in_red = self.red_above is not None and self.value >= self.red_above
            p.setBrush(badge[1] if badge else (_M_RED if in_red else _M_BLUE))
            x = bx0 + (bx1 - bx0) * self._frac(self.shown)
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
        self.setMinimumSize(78, 96)
        self._f_cap = _font(7.0, bold=True, spacing=0.5)
        self._fonts_d = None

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
                      int(-self.SPAN * 16 * self._frac(self.shown)))

        if self._fonts_d != d:
            self._f_val = _font(max(8.0, d * 0.20), bold=True)
            self._fonts_d = d
        p.setPen(_M_DIM_TEXT if dim else (badge[1] if badge else _M_TEXT))
        p.setFont(self._f_val)
        p.drawText(rect, Qt.AlignCenter, self._fmt(self.value))

        # caption: freshness badge when present, otherwise "LABEL unit"
        if badge is not None and not dim:
            p.setPen(badge[1])
            cap = badge[0]
        else:
            p.setPen(_M_DIM)
            cap = f"{self.label} {self.unit}".strip()
        p.setFont(self._f_cap)
        p.drawText(QRectF(0, cy + d / 2 + 3, w, 14),
                   Qt.AlignHCenter | Qt.AlignTop, cap)
        if badge is None and self._is_live():
            self._live_dot(p, w - 7.0, 7.0)
        p.end()


class ModernDial(GaugeBase):
    """Large modern dial: dark face, blue value arc, thin floating needle, and
    a big digital readout in the center (RPM / Speed)."""

    SWEEP_START = 225.0   # degrees, math orientation (0=east, CCW+)
    SWEEP = 270.0         # clockwise sweep from SWEEP_START

    def __init__(self, key, label, sublabel, vmin, vmax,
                 divisor=1.0, red_from=None, collapsible=False):
        # the unit slot carries the sublabel drawn under the digits
        super().__init__(key, label, sublabel, vmin, vmax,
                         collapsible=collapsible)
        self.divisor = float(divisor)
        self.red_from = red_from
        self.setMinimumSize(200, 200)
        self._fonts_side = None

    def _angle(self, v: float) -> float:
        return self.SWEEP_START - self.SWEEP * self._frac(v)

    def _pt(self, cx, cy, r, ang_deg):
        a = math.radians(ang_deg)
        return QPointF(cx + r * math.cos(a), cy - r * math.sin(a))

    def _geom(self, w, h):
        side = min(w, h)
        return side, w / 2.0, h / 2.0, side / 2.0 - 3

    def _fmt_digits(self) -> str:
        if self.value is None or not math.isfinite(self.value):
            return "—"
        if self.divisor > 1:
            return f"{self.value / self.divisor:.1f}"
        return f"{self.value:.0f}"

    def _paint_static(self, p, w, h):
        side, cx, cy, r = self._geom(w, h)
        dim = not self.available

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

        # sweep track (the value arc is dynamic, drawn over it)
        arc_r = r * 0.90
        arc_rect = QRectF(cx - arc_r, cy - arc_r, 2 * arc_r, 2 * arc_r)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(22, 25, 31) if dim else QColor(30, 37, 48),
                      max(3.0, side * 0.028), Qt.SolidLine, Qt.FlatCap))
        p.drawArc(arc_rect, int(self.SWEEP_START * 16), int(-self.SWEEP * 16))

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
        p.setFont(_font(max(6.0, side * 0.048), bold=True))
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

        # sublabel under the digits
        p.setPen(_M_DIM)
        p.setFont(_font(max(6.5, side * 0.042), bold=True))
        p.drawText(QRectF(cx - r, cy + side * 0.09, 2 * r, side * 0.09),
                   Qt.AlignCenter, self.unit)

    def _dyn_fonts(self, side):
        if self._fonts_side != side:
            self._f_digits = _font(max(12.0, side * 0.155), bold=True)
            self._f_mm = _font(max(5.5, side * 0.036))
            self._f_badge = _font(max(5.5, side * 0.040), bold=True)
            self._fonts_side = side
        return self._f_digits, self._f_mm, self._f_badge

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        side, cx, cy, r = self._geom(w, h)
        dim = not self.available
        badge = self._state_badge()
        self._draw_static(p, w, h)
        f_digits, f_mm, f_badge = self._dyn_fonts(side)

        # value arc at the eased position
        if self.value is not None and not dim:
            arc_r = r * 0.90
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(badge[1] if badge else _M_BLUE,
                          max(3.0, side * 0.028), Qt.SolidLine, Qt.FlatCap))
            p.drawArc(QRectF(cx - arc_r, cy - arc_r, 2 * arc_r, 2 * arc_r),
                      int(self.SWEEP_START * 16),
                      int(-self.SWEEP * 16 * self._frac(self.shown)))

        # big digital readout — the true latest sample
        p.setPen(_M_DIM_TEXT if dim else (badge[1] if badge else _M_TEXT))
        p.setFont(f_digits)
        p.drawText(QRectF(cx - r, cy - side * 0.16, 2 * r, side * 0.24),
                   Qt.AlignCenter, self._fmt_digits())

        # min/max capture + freshness slot
        if self.max_seen is not None:
            p.setPen(QColor(130, 136, 146))
            p.setFont(f_mm)
            p.drawText(QRectF(cx - r, cy + r * 0.52, 2 * r, side * 0.09),
                       Qt.AlignCenter,
                       f"▼{self.min_seen:.0f} ▲{self.max_seen:.0f}")
        slot = QRectF(cx - r, cy - r * 0.62, 2 * r, side * 0.10)
        if badge is not None:
            p.setPen(badge[1])
            p.setFont(f_badge)
            p.drawText(slot, Qt.AlignCenter, badge[0])
        elif self._is_live():
            p.setPen(LIVE_COLOR)
            p.setFont(f_badge)
            p.drawText(slot, Qt.AlignCenter, "● live")

        # thin floating needle (outer segment only, keeps the digits clear);
        # parked at vmin when no data yet
        ang = self._angle(self.shown)
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


# --------------------------------------------------------------------------- #
# Responsive strip layout
# --------------------------------------------------------------------------- #
class GaugeGridLayout(QLayout):
    """Uniform-cell grid that reflows with the available width: as many
    columns as fit the widest child's minimum width, rows balanced, a short
    last row centered. Hidden (collapsed) gauges take no cell. Height follows
    width (heightForWidth), so a narrow window grows the strip downward
    instead of forcing the whole window wide."""

    def __init__(self, parent: Optional[QWidget] = None, spacing: int = 4):
        super().__init__(parent)
        self._items: list = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    # -- QLayout plumbing --------------------------------------------------- #
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Horizontal | Qt.Vertical

    def hasHeightForWidth(self):
        return True

    # -- geometry ----------------------------------------------------------- #
    def _visible(self):
        return [it for it in self._items if not it.isEmpty()]

    def _cell_min(self, items) -> QSize:
        w = h = 0
        for it in items or self._items:
            m = it.minimumSize()
            w, h = max(w, m.width()), max(h, m.height())
        return QSize(max(1, w), max(1, h))

    def _margins(self):
        m = self.contentsMargins()
        return m.left(), m.top(), m.right(), m.bottom()

    def heightForWidth(self, width):
        items = self._visible()
        if not items:
            return 0
        ml, mt, mr, mb = self._margins()
        cell = self._cell_min(items)
        _cols, rows = grid_shape(len(items), width - ml - mr, cell.width(),
                                 self.spacing())
        return rows * cell.height() + (rows - 1) * self.spacing() + mt + mb

    def minimumSize(self):
        ml, mt, mr, mb = self._margins()
        cell = self._cell_min(self._visible())
        return QSize(cell.width() + ml + mr, cell.height() + mt + mb)

    def sizeHint(self):
        items = self._visible()
        ml, mt, mr, mb = self._margins()
        cell = self._cell_min(items)
        n = max(1, len(items))
        return QSize(n * cell.width() + (n - 1) * self.spacing() + ml + mr,
                     cell.height() + mt + mb)

    def setGeometry(self, rect: QRect):
        super().setGeometry(rect)
        items = self._visible()
        if not items:
            return
        ml, mt, mr, mb = self._margins()
        x, y = rect.x() + ml, rect.y() + mt
        w, h = rect.width() - ml - mr, rect.height() - mt - mb
        cell = self._cell_min(items)
        cols, rows = grid_shape(len(items), w, cell.width(), self.spacing())
        for it, (cx, cy, cw, ch) in zip(
                items, grid_cells(len(items), x, y, w, h, cols, rows,
                                  self.spacing())):
            it.setGeometry(QRect(int(round(cx)), int(round(cy)),
                                 int(cw), int(ch)))


# --------------------------------------------------------------------------- #
# Cluster builders (the Dashboard owns the gauges dict; these fill it)
# --------------------------------------------------------------------------- #
def build_classic_cluster(gauges: dict) -> QWidget:
    host = QWidget()
    cluster = QVBoxLayout(host)
    cluster.setContentsMargins(0, 0, 0, 0)
    cluster.setSpacing(4)

    dials = {}
    for key, label, unit, lo, hi, large, div, yel, red in DIAL_SPECS:
        # the two large dials anchor the cluster: dim, never collapse
        dials[key] = DialGauge(key, label, unit, lo, hi, large=large,
                               divisor=div, yellow_from=yel, red_from=red,
                               collapsible=not large)
        gauges[key] = dials[key]

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

    bar_grid = GaugeGridLayout(spacing=4)
    for key, label, unit, lo, hi, ra, rb in BAR_SPECS:
        g = BarGauge(key, label, unit, lo, hi, red_above=ra, red_below=rb)
        gauges[key] = g
        bar_grid.addWidget(g)
    cluster.addLayout(bar_grid, 2)
    return host


def build_modern_cluster(gauges: dict) -> QWidget:
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
        gauges[key] = g
        left.addWidget(g, 1)
    top.addLayout(left, 2)

    dials = []
    for key, label, sub, lo, hi, div, red in MODERN_DIAL_SPECS:
        g = ModernDial(key, label, sub, lo, hi, divisor=div, red_from=red)
        gauges[key] = g
        dials.append(g)
    top.addWidget(dials[0], 4)

    center = QVBoxLayout()
    center.setSpacing(6)
    for key, label, unit in MODERN_CENTER_SPECS:
        g = ModernTile(key, label, unit, centered=True)
        gauges[key] = g
        center.addWidget(g, 1)
    top.addLayout(center, 2)

    top.addWidget(dials[1], 4)

    right = QVBoxLayout()
    right.setSpacing(6)
    for key, label, unit, lo, hi, ra in MODERN_BAR_SPECS:
        g = ModernBarTile(key, label, unit, lo, hi, red_above=ra)
        gauges[key] = g
        right.addWidget(g, 1)
    top.addLayout(right, 3)
    cluster.addLayout(top, 3)

    ring_grid = GaugeGridLayout(spacing=6)
    for key, label, unit, lo, hi, ra, rb in MODERN_RING_SPECS:
        g = ModernRing(key, label, unit, lo, hi,
                       red_above=ra, red_below=rb)
        gauges[key] = g
        ring_grid.addWidget(g)
    cluster.addLayout(ring_grid, 1)
    return host
