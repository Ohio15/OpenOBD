"""
pedal -- accelerator pedal normalization: the calibration and how it is learned.

PID 49 (APP D) is the pedal SENSOR's position, not the driver's demand: on the
2026-10-25 drive (gt_log_20261025_141431.csv) it read 19 % with the foot off
and peaked at 61 % on a pull that reached 5632 rpm, MAP 99 kPa and 100 %
calculated load. Every "pedal >= 70" WOT test on the raw value therefore found
nothing. The fix is a DERIVED channel, "pedal":

    pedal = clamp((app - rest) / (full - rest) * 100, 0, 100)

where rest and full are LEARNED from this vehicle's own data, never assumed:

  rest  the minimum APP D seen while the engine is below REST_RPM_MAX and the
        truck is stopped (vss == 0), once that state has held for at least
        REST_HOLD_S seconds;
  full  the maximum APP D seen while there is true WOT evidence: calculated
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

#: Settings key used when the vehicle could not be identified (no VIN).
UNKNOWN_VIN = "unknown-vin"

RULES = (f"rest = min APP D while rpm < {REST_RPM_MAX:g} and vss == 0 held "
         f">= {REST_HOLD_S:g} s; full = max APP D while (load >= "
         f"{WOT_LOAD_MIN:g} % or MAP >= {WOT_MAP_MIN:g} kPa) and rpm >= "
         f"{ENGINE_RUNNING_RPM:g}; full only raised")


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
                return None if v is None else float(v)
            except (TypeError, ValueError):
                return None
        return cls(num(d.get("rest")), num(d.get("full")),
                   str(d.get("source") or ""))


@dataclass
class PedalLearner:
    """Streaming learner for one connection (or one log pass).

    observe() takes one operating point at a time, in time order. A point
    with rpm, vss or app missing breaks a rest stretch (unknown is never
    "still at rest"). `cal` is the calibration in force: seeded values plus
    what this pass learned (rest: this pass's own minimum replaces a seed
    once learned, because rest drifts with the sensor; full: the maximum of
    seed and learned). `version` increments whenever `cal` changes."""
    cal: PedalCal = field(default_factory=PedalCal)
    version: int = 0
    learned_rest: Optional[float] = None
    learned_full: Optional[float] = None
    _seed_source: str = ""
    _learn_source: str = "learned"
    _stretch_t0: Optional[float] = None
    _stretch_min: Optional[float] = None

    def seed(self, cal: PedalCal) -> None:
        """Merge a stored calibration in: a rest already learned on this pass
        wins over the seed; full is the larger of the two."""
        self._seed_source = cal.source or "seed"
        rest = self.learned_rest if self.learned_rest is not None else cal.rest
        fulls = [v for v in (cal.full, self.learned_full) if v is not None]
        full = max(fulls) if fulls else None
        self._set(rest, full, self._label())

    def _label(self) -> str:
        learned = self.learned_rest is not None or self.learned_full is not None
        if self._seed_source and learned:
            return f"{self._seed_source} + {self._learn_source}"
        return self._seed_source or (self._learn_source if learned else "")

    def _set(self, rest, full, source) -> None:
        if (rest, full) != (self.cal.rest, self.cal.full) or source != self.cal.source:
            self.cal = PedalCal(rest, full, source)
            self.version += 1

    def observe(self, t: float, rpm: Optional[float], vss: Optional[float],
                app: Optional[float], load: Optional[float] = None,
                map_kpa: Optional[float] = None, source: str = "learned") -> bool:
        """Fold one operating point in; True when the calibration changed."""
        before = self.version
        rest, full = self.cal.rest, self.cal.full
        at_rest = (rpm is not None and vss is not None and app is not None
                   and rpm < REST_RPM_MAX and vss == 0)
        if at_rest:
            if self._stretch_t0 is None:
                self._stretch_t0, self._stretch_min = t, app
            else:
                self._stretch_min = min(self._stretch_min, app)
            if t - self._stretch_t0 >= REST_HOLD_S:
                if self.learned_rest is None or self._stretch_min < self.learned_rest:
                    self.learned_rest = self._stretch_min
                rest = self.learned_rest
        else:
            self._stretch_t0 = self._stretch_min = None
        if app is not None and is_wot_evidence(rpm, load, map_kpa):
            if self.learned_full is None or app > self.learned_full:
                self.learned_full = app
            if full is None or app > full:
                full = app
        self._learn_source = source
        self._set(rest, full, self._label())
        return self.version != before


def calibrate(points: Iterable[tuple], source: str) -> PedalCal:
    """Calibration from (t, rpm, vss, app, load, map) points in time order,
    with exactly the live rules. Either value may stay None (not learned)."""
    learner = PedalLearner()
    for t, rpm, vss, app, load, map_kpa in points:
        if t is None:
            continue
        learner.observe(t, rpm, vss, app, load, map_kpa, source=source)
    return PedalCal(learner.learned_rest, learner.learned_full, source)
