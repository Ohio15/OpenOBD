"""
pedal -- accelerator pedal normalization: the calibration and how it is learned.

PID 49 (APP D) is the pedal SENSOR's position, not the driver's demand: on the
2026-10-25 drive (gt_log_20261025_141431.csv) it read 19 % with the foot off
and peaked at 61 % on a pull that reached 5632 rpm, MAP 99 kPa and 100 %
calculated load. Every "pedal >= 70" WOT test on the raw value therefore found
nothing. The fix is a DERIVED channel, "pedal":

    pedal = clamp((app - rest) / (full - rest) * 100, 0, 100)

where rest and full are LEARNED from this vehicle's own data, never assumed:

  rest  the MEDIAN APP D over a stretch where the engine is below
        REST_RPM_MAX and the truck is stopped (vss == 0), once that state
        has held for at least REST_HOLD_S seconds of observed samples;
        later stretches only lower it;
  full  the maximum APP D over a run of at least WOT_RUN_MIN consecutive
        samples with true WOT evidence: calculated
        load >= WOT_LOAD_MIN or MAP >= WOT_MAP_MIN kPa, with the engine
        running (rpm >= ENGINE_RUNNING_RPM). The rpm guard is not in the
        brief's wording but is required by physics: with the key on and the
        engine off, MAP reads barometric (~99-101 kPa), which would otherwise
        count as "WOT" and learn whatever the pedal happened to read. full is
        only ever RAISED by learning, never lowered automatically.

Until both are known (and full is at least MIN_SPAN above rest) the derived
channel does not exist: no value is ever computed from a pending calibration.
The raw APP D channels are always recorded exactly as decoded.

Pure: no Qt, no serial. The transport (live), logbin (logs) and the app
(persistence keyed by VIN) all use this one implementation of the rules.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Iterable, Optional

REST_RPM_MAX = 1200.0
REST_HOLD_S = 2.0
WOT_LOAD_MIN = 98.0
WOT_MAP_MIN = 95.0
ENGINE_RUNNING_RPM = 400.0
#: Smallest rest->full span accepted as a calibration (APP D %). Below it the
#: division would amplify sensor noise into full-scale swings.
MIN_SPAN = 5.0

#: Consecutive WOT-evidence points a pull needs before its APP D counts.
WOT_RUN_MIN = 3

RULES = (f"rest = median APP D over a stretch with rpm < {REST_RPM_MAX:g} and "
         f"vss == 0 held >= {REST_HOLD_S:g} s of observed samples, only "
         f"lowered; full = max APP D over >= {WOT_RUN_MIN} consecutive samples "
         f"with (load >= {WOT_LOAD_MIN:g} % or MAP >= {WOT_MAP_MIN:g} kPa) "
         f"and rpm >= {ENGINE_RUNNING_RPM:g}, only raised")


def is_wot_evidence(rpm: Optional[float], load: Optional[float],
                    map_kpa: Optional[float]) -> bool:
    """True WOT evidence that does not depend on the pedal sensor."""
    if rpm is None or rpm < ENGINE_RUNNING_RPM:
        return False
    return ((load is not None and load >= WOT_LOAD_MIN)
            or (map_kpa is not None and map_kpa >= WOT_MAP_MIN))


@dataclass
class PedalCal:
    """A pedal calibration. source says where each value came from (learned
    live on a connection, imported from a log, loaded from settings)."""
    rest: Optional[float] = None
    full: Optional[float] = None
    source: str = ""

    @property
    def ready(self) -> bool:
        return (self.rest is not None and self.full is not None
                and self.full - self.rest >= MIN_SPAN)

    def normalize(self, app: Optional[float]) -> Optional[float]:
        """Normalized pedal %, or None when the calibration is pending or
        there is no raw value. Never a value from a pending calibration."""
        if app is None or not self.ready:
            return None
        pct = (app - self.rest) / (self.full - self.rest) * 100.0
        return max(0.0, min(100.0, pct))

    def status(self) -> str:
        if self.ready:
            return f"rest {self.rest:.1f} % / full {self.full:.1f} % APP D"
        missing = [n for n, v in (("rest", self.rest), ("full", self.full))
                   if v is None]
        if missing:
            return "pedal cal pending (" + ", ".join(missing) + " not learned)"
        return (f"pedal cal pending (full {self.full:.1f} is less than "
                f"{MIN_SPAN:g} above rest {self.rest:.1f})")

    def to_dict(self) -> dict:
        return {"rest": self.rest, "full": self.full, "source": self.source,
                "ready": self.ready, "rules": RULES}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "PedalCal":
        if not isinstance(d, dict):
            return cls()

        def num(v):
            try:
                f = None if v is None else float(v)
            except (TypeError, ValueError):
                return None
            # NaN / inf is not a calibration value: treated as pending.
            return f if f is not None and math.isfinite(f) else None
        return cls(num(d.get("rest")), num(d.get("full")),
                   str(d.get("source") or ""))


@dataclass
class PedalLearner:
    """Streaming learner for one connection (or one log pass).

    observe() takes one operating point at a time, in time order.

    rest   the MEDIAN of APP D over a stopped low-rpm stretch once it has
           held REST_HOLD_S over OBSERVED points (a single glitch cannot move
           it); a later stretch only ever LOWERS it. A point with rpm, vss or
           app missing, a gap between observed points longer than max_gap,
           or an explicit gap() (a failed poll) breaks the stretch: time the
           learner did not see is never counted as "still at rest".
    full   the MAX of APP D over a run of at least WOT_RUN_MIN consecutive
           WOT-evidence points (a one-sample spike is not a pull), only ever
           raised. Gaps break a run the same way.

    `cal` is the calibration in force: a learned rest replaces a seeded one
    (rest drifts with the sensor), full is the larger of seed and learned.
    reset() forgets everything (a new connection may be another vehicle).
    `version` increments whenever `cal` changes, and on reset()."""
    cal: PedalCal = field(default_factory=PedalCal)
    version: int = 0
    learned_rest: Optional[float] = None
    learned_full: Optional[float] = None
    _seed: Optional[PedalCal] = None
    _learn_source: str = "learned"
    _stretch_t0: Optional[float] = None
    _stretch_vals: list = field(default_factory=list)
    _wot_run: list = field(default_factory=list)
    _last_t: Optional[float] = None

    def reset(self) -> None:
        """Forget the seed, everything learned and any partial stretch/run."""
        self.learned_rest = self.learned_full = None
        self._seed = None
        self.gap()
        self._last_t = None
        self.cal = PedalCal()
        self.version += 1

    def gap(self) -> None:
        """The learner missed time (failed poll, outage): no stretch or run
        continues across it."""
        self._stretch_t0 = None
        self._stretch_vals = []
        self._wot_run = []

    def seed(self, cal: PedalCal) -> None:
        """Merge a stored calibration in: a rest already learned on this pass
        wins over the seed; full is the larger of the two."""
        self._seed = PedalCal(cal.rest, cal.full, cal.source or "seed")
        self._refresh()

    def _label(self) -> str:
        learned = self.learned_rest is not None or self.learned_full is not None
        seed = self._seed.source if self._seed else ""
        if seed and learned:
            return f"{seed} + {self._learn_source}"
        return seed or (self._learn_source if learned else "")

    def _refresh(self) -> None:
        seed = self._seed or PedalCal()
        rest = self.learned_rest if self.learned_rest is not None else seed.rest
        fulls = [v for v in (seed.full, self.learned_full) if v is not None]
        full = max(fulls) if fulls else None
        source = self._label()
        if (rest, full, source) != (self.cal.rest, self.cal.full,
                                    self.cal.source):
            self.cal = PedalCal(rest, full, source)
            self.version += 1

    def observe(self, t: float, rpm: Optional[float], vss: Optional[float],
                app: Optional[float], load: Optional[float] = None,
                map_kpa: Optional[float] = None, source: str = "learned",
                max_gap: Optional[float] = None) -> bool:
        """Fold one operating point in; True when the calibration changed.
        max_gap: the longest spacing between observed points that still
        counts as continuous (the caller knows its sample period)."""
        before = self.version
        if (max_gap is not None and self._last_t is not None
                and t - self._last_t > max_gap):
            self.gap()
        self._last_t = t
        at_rest = (rpm is not None and vss is not None and app is not None
                   and rpm < REST_RPM_MAX and vss == 0)
        if at_rest:
            if self._stretch_t0 is None:
                self._stretch_t0 = t
            self._stretch_vals.append(app)
            if t - self._stretch_t0 >= REST_HOLD_S:
                med = statistics.median(self._stretch_vals)
                if self.learned_rest is None or med < self.learned_rest:
                    self.learned_rest = med
        else:
            self._stretch_t0 = None
            self._stretch_vals = []
        if app is not None and is_wot_evidence(rpm, load, map_kpa):
            self._wot_run.append(app)
            if len(self._wot_run) >= WOT_RUN_MIN:
                top = max(self._wot_run)
                if self.learned_full is None or top > self.learned_full:
                    self.learned_full = top
        else:
            self._wot_run = []
        self._learn_source = source
        self._refresh()
        return self.version != before


def typical_gap(times: list) -> Optional[float]:
    """Continuity limit for a series: twice its median spacing."""
    gaps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    return 2.0 * gaps[len(gaps) // 2] if gaps else None


def calibrate(points: Iterable[tuple], source: str) -> PedalCal:
    """Calibration from (t, rpm, vss, app, load, map) points in time order,
    with exactly the live rules; continuity is twice the points' own median
    spacing (a 0.7 Hz log and a 4 Hz log are both judged on their own
    rate). Either value may stay None (not learned)."""
    pts = [p for p in points if p[0] is not None]
    max_gap = typical_gap([p[0] for p in pts])
    learner = PedalLearner()
    for t, rpm, vss, app, load, map_kpa in pts:
        learner.observe(t, rpm, vss, app, load, map_kpa, source=source,
                        max_gap=max_gap)
    return PedalCal(learner.learned_rest, learner.learned_full, source)
