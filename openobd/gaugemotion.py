"""
gaugemotion — the Qt-free logic behind the Dashboard's fluent gauges.

Three concerns, all pure (time is always injected, never read here), so they
can be unit-tested without a display:

  * NeedleMotion — time-based exponential easing of a needle/arc/bar position
    toward the latest real value. Easing is VISUAL ONLY: it moves geometry,
    never the number. The readout always prints the true latest sample; the
    eased position is never presented as data. Exponential approach cannot
    overshoot, so the needle only ever travels between two real readings.
  * LiveFreshness — per-channel fresh/carried/stale classification for live
    sources whose own vocabulary is coarse (the OBDX GT reports only
    fresh/failed for the whole link). Uses tmstore's state names so both live
    paths render the same badges.
  * Readout formatting and the responsive grid shape for gauge strips.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

#: Easing time constant. 95% of a step is covered in ~3*tau (~0.27 s): fast
#: enough that the needle never lags a 10 Hz replay noticeably, slow enough
#: that a 1 Hz GT poll reads as motion instead of a jump.
EASE_TAU_S = 0.09
#: Below this fraction of the gauge span the needle snaps to the target and
#: animation stops, so a settled cluster costs no frames at all.
SETTLE_FRAC = 0.0015
#: dt used for the first frame of a motion, before a frame time exists.
FIRST_FRAME_DT_S = 1.0 / 60.0


def ease_toward(shown: float, target: float, dt: float,
                tau: float = EASE_TAU_S) -> float:
    """One frame of exponential approach: the fraction of the remaining gap
    covered depends only on elapsed time, so the motion looks identical at
    30, 60 or 144 fps and under timer jitter."""
    if dt <= 0.0:
        return shown
    if tau <= 0.0:
        return target
    # A long gap (window dragged, timer starved) yields k ~ 1: the needle
    # lands on the value instead of crawling through stale history.
    k = 1.0 - math.exp(-dt / tau)
    return shown + (target - shown) * k


class NeedleMotion:
    """Displayed position for one gauge, eased toward a clamped target.

    The target is clamped to [vmin, vmax] because the geometry clamps anyway:
    easing toward an off-scale 8000 rpm would pin the needle at max and then
    delay its return by however long it takes to "travel back" through the
    invisible overrange. None parks the needle at vmin (no data)."""

    __slots__ = ("vmin", "vmax", "tau", "shown", "target", "_last_t", "_eps")

    def __init__(self, vmin: float, vmax: float, tau: float = EASE_TAU_S):
        self.vmin = float(vmin)
        self.vmax = float(vmax)
        self.tau = tau
        self.shown = self.vmin
        self.target = self.vmin
        self._last_t: Optional[float] = None
        self._eps = abs(self.vmax - self.vmin) * SETTLE_FRAC or SETTLE_FRAC

    def _clamp(self, v: float) -> float:
        lo, hi = min(self.vmin, self.vmax), max(self.vmin, self.vmax)
        return lo if v < lo else hi if v > hi else v

    def set_target(self, value: Optional[float]) -> bool:
        """Returns True when the target moved (the caller schedules frames)."""
        if value is None or not math.isfinite(value):
            tgt = self.vmin
        else:
            tgt = self._clamp(float(value))
        if tgt == self.target:
            return False
        self.target = tgt
        return self.moving

    @property
    def moving(self) -> bool:
        return self.shown != self.target

    def step(self, now: float) -> bool:
        """Advance to wall time `now`. Returns True when `shown` changed (the
        caller repaints only then)."""
        if not self.moving:
            self._last_t = None
            return False
        dt = FIRST_FRAME_DT_S if self._last_t is None else now - self._last_t
        self._last_t = now
        new = ease_toward(self.shown, self.target, dt, self.tau)
        if abs(self.target - new) <= self._eps:
            new = self.target
        changed = new != self.shown
        self.shown = new
        if not self.moving:
            self._last_t = None
        return changed

    def snap(self) -> None:
        """Jump to the target (hidden widget, reset): no animation owed."""
        self.shown = self.target
        self._last_t = None

    def reset(self) -> None:
        self.target = self.shown = self.vmin
        self._last_t = None


# --------------------------------------------------------------------------- #
# Readouts
# --------------------------------------------------------------------------- #
#: Channels whose readings are inherently whole numbers.
INTEGER_CHANNELS = frozenset({"gear"})


def reading_decimals(vmin: float, vmax: float, key: str = "") -> int:
    """Fixed decimals per gauge so the readout width doesn't jitter between
    samples (":g" printed 13.7255 then 14 then 13.9216). Wide scales (rpm,
    speed, temperatures — spans of 150+) read in whole units; everything
    narrower (MAF at idle, kPa, percent, volts, trims, knock retard) to a
    tenth, where a whole-unit readout would hide real movement."""
    if key in INTEGER_CHANNELS:
        return 0
    return 0 if abs(vmax - vmin) >= 150 else 1


def fmt_reading(value: Optional[float], decimals: int) -> str:
    """The true latest sample, rounded for display only. "—" for no data."""
    if value is None or not math.isfinite(value):
        return "—"
    txt = f"{value:.{decimals}f}"
    # "-0" / "-0.0" is rounding noise around zero, not a negative reading.
    if txt.startswith("-") and float(txt) == 0.0:
        txt = txt[1:]
    return txt


# --------------------------------------------------------------------------- #
# Live freshness for sources with a coarse (link-level) vocabulary
# --------------------------------------------------------------------------- #
#: Never call a channel stale sooner than this — the first GT polls, before
#: the interval estimate exists, must not flash "not advancing".
STALE_FLOOR_S = 2.0
#: ... and never wait longer than this before saying so.
STALE_CAP_S = 10.0
#: A channel is stale after this many typical poll intervals without news.
STALE_FACTOR = 3.0
_EMA_ALPHA = 0.3


class LiveFreshness:
    """Per-channel freshness derived from sample arrival on the wall clock.

    The GT polls every PID in sequence, so a full round takes ~0.5-2 s and a
    PID that times out is simply absent from that round's sample. Before this
    existed the cluster rendered the absent channel as "—" with the needle
    dropped to zero for one round (a flicker every missed PID), and a link
    that kept answering with empty rounds stayed "fresh" forever.

    States (tmstore vocabulary, so the cluster's badges already know them):
      fresh   — present in the newest sample, and samples are arriving
      carried — absent from the newest sample; showing its last real reading
      stale   — no new sample for longer than stale_after(); last reading held
      unavail — absent for longer than stale_after(); value withdrawn
    A non-fresh state from the source itself (the GT's "failed") always wins.
    """

    def __init__(self, floor_s: float = STALE_FLOOR_S,
                 cap_s: float = STALE_CAP_S, factor: float = STALE_FACTOR):
        self.floor_s = floor_s
        self.cap_s = cap_s
        self.factor = factor
        self._last_sample_t: Optional[float] = None
        self._newest_at: Optional[float] = None
        self._newest_keys: frozenset = frozenset()
        self._interval: Optional[float] = None
        self._last: dict[str, tuple[float, float]] = {}   # key -> (value, seen_at)

    def observe(self, sample_t: Optional[float],
                values: Optional[dict], now: float) -> bool:
        """Feed the source's latest() each tick (the same sample may repeat).
        A sample counts as new when its timestamp changed. Returns True for a
        new sample."""
        if sample_t is None or values is None or sample_t == self._last_sample_t:
            return False
        if self._newest_at is not None:
            gap = now - self._newest_at
            if gap > 0:
                self._interval = (gap if self._interval is None else
                                  self._interval + _EMA_ALPHA * (gap - self._interval))
        self._last_sample_t = sample_t
        self._newest_at = now
        keys = []
        for k, v in values.items():
            if v is None:
                continue
            self._last[k] = (v, now)
            keys.append(k)
        self._newest_keys = frozenset(keys)
        return True

    def stale_after(self) -> float:
        if self._interval is None:
            return self.floor_s
        return max(self.floor_s, min(self.cap_s, self.factor * self._interval))

    def resolve(self, key: str, source_state: Optional[str],
                now: float) -> tuple[Optional[str], Optional[float]]:
        """(state, value-to-display) for one channel."""
        if source_state not in (None, "fresh"):
            if source_state == "failed":
                return source_state, None
            last = self._last.get(key)
            return source_state, (last[0] if last else None)
        if self._newest_at is None:
            return source_state, None          # nothing has arrived yet
        limit = self.stale_after()
        last = self._last.get(key)
        if now - self._newest_at > limit:
            return "stale", (last[0] if last else None)
        if key in self._newest_keys:
            return "fresh", last[0]
        if last is None:
            return None, None                  # never answered: plain "—"
        if now - last[1] <= limit:
            return "carried", last[0]
        return "unavail", None

    def resolve_all(self, keys: Iterable[str], source_states: Optional[dict],
                    now: float) -> tuple[dict, dict]:
        states: dict[str, Optional[str]] = {}
        values: dict[str, Optional[float]] = {}
        for k in keys:
            src = source_states.get(k) if source_states is not None else None
            states[k], values[k] = self.resolve(k, src, now)
        return states, values


# --------------------------------------------------------------------------- #
# Responsive strip layout
# --------------------------------------------------------------------------- #
def grid_shape(n: int, avail_w: float, cell_min_w: float,
               spacing: float) -> tuple[int, int]:
    """(columns, rows) for n uniform cells in avail_w pixels. As many columns
    as fit at the minimum cell width, then rebalanced so rows are as even as
    possible (12 cells that fit 7-wide lay out 6+6, not 7+5)."""
    if n <= 0:
        return 0, 0
    fit = int((max(0.0, avail_w) + spacing) // max(1.0, cell_min_w + spacing))
    cols = max(1, min(n, fit))
    rows = -(-n // cols)
    cols = -(-n // rows)
    return cols, rows


def grid_cells(n: int, x: float, y: float, w: float, h: float,
               cols: int, rows: int, spacing: float
               ) -> list[tuple[float, float, float, float]]:
    """Cell rects (x, y, w, h) filling the box row-major; a short last row is
    centered so the strip stays visually balanced."""
    if n <= 0 or cols <= 0 or rows <= 0:
        return []
    cw = max(0.0, (w - (cols - 1) * spacing) / cols)
    ch = max(0.0, (h - (rows - 1) * spacing) / rows)
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        in_row = min(cols, n - r * cols)
        x0 = x + (cols - in_row) * (cw + spacing) / 2.0
        out.append((x0 + c * (cw + spacing), y + r * (ch + spacing), cw, ch))
    return out
