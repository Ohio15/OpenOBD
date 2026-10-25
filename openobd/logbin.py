"""
logbin — log ingestion + the "log over table" overlay engine.

This is the core that makes the viewer feel like HP Tuners: load a log, and for
any table, drop every sample into the cell whose axes it falls in, then show a
histogram of where the engine actually operated and the mean measured value in
each cell.

It also computes the regime stats codified from the 8.8.26 baseline analysis
(idle/cruise/PE trims, knock-retard events, fuel-pressure sag) — the same logic
`wot_analyzer` in the GT spec is meant to expose. GUI-free and unit-tested.

Supported inputs
----------------
1. VCM Scanner CSV export:
       [Log Information] ... / [Channel Information] (id,name,unit rows) /
       [Channel Data] (samples)
2. Plain CSV with a single header row of channel names.

Channel-name mapping is fuzzy (case/space/paren-insensitive substring) so both
HP Tuners' verbose names and our own short names resolve to canonical keys.
"""
from __future__ import annotations

import bisect
import csv
import io
import math
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .calspec import Table


# --------------------------------------------------------------------------- #
# Canonical channel vocabulary. Each canonical key lists substrings that, if
# any appears in a log's channel name (normalized), maps that column to the key.
# Order matters: more specific keys should be tested before generic ones.
# --------------------------------------------------------------------------- #
CANONICAL = {
    "time":        ["time", "offset", "timestamp"],
    "rpm":         ["engine speed", "rpm", "enginerpm"],
    "vss":         ["vehicle speed", "vss", "vehiclespeed"],
    "map":         ["manifold absolute", "map"],
    "maf":         ["mass air flow", "maf", "airflow"],
    # PID 43 absolute load is NOT calculated load (it can exceed 100 %), so
    # it is matched before "load", and "absload" moved here from "load".
    "abs_load":    ["absolute load", "abs load", "absload"],
    "load":        ["engine load", "calculated load", "load"],
    # Relative throttle and pedal sensors E/F before the generic needles
    # ("throttle position", "accelerator pedal") that would swallow them.
    "rel_tps":     ["relative throttle", "rel tps", "reltps"],
    "tps":         ["throttle position", "tps"],
    "app_e":       ["accelerator pedal e", "pedal position e"],
    "app_f":       ["accelerator pedal f", "pedal position f"],
    "app":         ["accelerator pedal", "pedal position", "app"],
    "cmd_throttle":["commanded throttle", "throttle command"],
    # Bank 2 trims before the bank-agnostic "short/long term fuel" needles.
    "stft_b2":     ["stft bank 2", "stft b2", "short term fuel trim bank 2",
                    "bank 2 short term"],
    "ltft_b2":     ["ltft bank 2", "ltft b2", "long term fuel trim bank 2",
                    "bank 2 long term"],
    "stft":        ["short term fuel", "stft", "shorttermft"],
    "ltft":        ["long term fuel", "ltft", "longtermft"],
    "fuel_system": ["fuel system status", "fuel sys status"],
    # O2 sensor voltages and catalyst temps per position, before the generic
    # "o2 sensor" / "voltage" needles. Every needle names the sensor type, so
    # a B1S1 catalyst column can never land on the B1S1 O2 sensor.
    "o2_b1s1":     ["o2 voltage b1s1", "o2 sensor b1s1", "o2 b1s1"],
    "o2_b1s2":     ["o2 voltage b1s2", "o2 sensor b1s2", "o2 b1s2"],
    "o2_b2s1":     ["o2 voltage b2s1", "o2 sensor b2s1", "o2 b2s1"],
    "o2_b2s2":     ["o2 voltage b2s2", "o2 sensor b2s2", "o2 b2s2"],
    "cat_b1s1":    ["catalyst temp b1s1", "cat temp b1s1"],
    "cat_b2s1":    ["catalyst temp b2s1", "cat temp b2s1"],
    "eq_ratio":    ["equivalence", "eq ratio", "commanded eq"],
    "afr":         ["air fuel", "afr", "lambda"],
    "o2":          ["o2 sensor", "oxygen"],
    "spark":       ["spark advance", "ignition timing", "timing advance", "spark"],
    "knock_retard":["knock retard", "kr", "retard"],
    "iat":         ["intake air temp", "iat"],
    "ect":         ["coolant temp", "engine coolant", "ect"],
    "tft":         ["trans fluid temp", "transmission fluid", "tft"],
    "gear":        ["current gear", "gear"],
    "tcc_slip":    ["tcc slip", "converter slip", "slip"],
    "fuel_press":  ["fuel rail", "fuel pressure"],
    "fuel_level":  ["fuel level"],
    "ethanol":     ["ethanol", "flex fuel"],
    "voltage":     ["module voltage", "battery voltage", "system voltage", "voltage"],
    "baro":        ["barometric", "baro"],
    "ambient":     ["ambient air", "outside air"],
}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


def map_channel(name: str) -> Optional[str]:
    """Map a raw log channel name to a canonical key, or None."""
    n = _norm(name)
    for key, needles in CANONICAL.items():
        for needle in needles:
            if needle in n:
                return key
    return None


# --------------------------------------------------------------------------- #
# Parsed log
# --------------------------------------------------------------------------- #
@dataclass
class LogChannel:
    raw_name: str
    canonical: Optional[str]
    unit: str
    values: list[Optional[float]] = field(default_factory=list)


@dataclass
class Log:
    channels: list[LogChannel]
    n_samples: int
    source: str = ""
    #: Per-channel (t, value) readings from the long-format sidecar, when
    #: one was loaded next to the CSV (attach_long); None otherwise.
    timed: Optional[dict] = None

    def by_canonical(self, key: str) -> Optional[LogChannel]:
        for ch in self.channels:
            if ch.canonical == key:
                return ch
        return None

    def has(self, key: str) -> bool:
        return self.by_canonical(key) is not None

    def series(self, key: str) -> list[Optional[float]]:
        ch = self.by_canonical(key)
        return ch.values if ch else []

    def canonical_keys(self) -> list[str]:
        return [c.canonical for c in self.channels if c.canonical]


def time_axis(log: Log, fallback_dt: float = 0.1) -> list[float]:
    """Zero-based seconds for every sample: the log's time channel normalized
    to start at 0 (gaps hold the last value), or fallback_dt spacing when no
    time channel is present. Shared by the replay source and the chart view."""
    tser = log.series("time")
    if tser and any(v is not None for v in tser):
        base = next(v for v in tser if v is not None)
        out: list[float] = []
        last = 0.0
        for v in tser:
            if v is not None:
                last = v - base
            out.append(last)
        return out
    return [i * fallback_dt for i in range(log.n_samples)]


def _to_float(s: str) -> Optional[float]:
    s = s.strip()
    if s == "" or s.upper() in ("N/A", "NA", "---", "NAN"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_csv(text: str, source: str = "") -> Log:
    """
    Parse either a VCM Scanner sectioned CSV or a plain header+rows CSV.
    Returns a Log with canonical channel mapping applied.
    """
    # Detect the VCM Scanner sectioned format by its bracket headers.
    if "[Channel Data]" in text or "[Channel Information]" in text:
        return _parse_vcm_scanner(text, source)
    return _parse_plain(text, source)


def _parse_plain(text: str, source: str) -> Log:
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r]
    if not rows:
        return Log(channels=[], n_samples=0, source=source)
    header = rows[0]
    channels = [
        LogChannel(raw_name=h, canonical=map_channel(h), unit="")
        for h in header
    ]
    for row in rows[1:]:
        for i, ch in enumerate(channels):
            ch.values.append(_to_float(row[i]) if i < len(row) else None)
    n = len(rows) - 1
    return Log(channels=channels, n_samples=n, source=source)


def _parse_vcm_scanner(text: str, source: str) -> Log:
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader]
    # Find section markers.
    idx = {}
    for i, r in enumerate(rows):
        if r and r[0].strip().startswith("[") and r[0].strip().endswith("]"):
            idx[r[0].strip()] = i

    ci = idx.get("[Channel Information]")
    cd = idx.get("[Channel Data]")
    if ci is None or cd is None:
        # Fall back to plain parse of everything after the last bracket header.
        return _parse_plain(text, source)

    # Channel Information block = rows between [Channel Information] and
    # [Channel Data]. HP Tuners emits an id row, a name row, and a unit row.
    info = [r for r in rows[ci + 1:cd] if r]
    # Heuristic: the row containing recognizable channel names is the name row;
    # the last non-empty info row before data is typically units.
    name_row = None
    unit_row = None
    if len(info) >= 2:
        # pick the row with the most alphabetic content as the name row
        def alpha_score(r):
            return sum(any(c.isalpha() for c in cell) for cell in r)
        info_sorted = sorted(info, key=alpha_score, reverse=True)
        name_row = info_sorted[0]
        # unit row: the info row (other than name) with short tokens
        for r in info:
            if r is not name_row:
                unit_row = r
                break
    elif len(info) == 1:
        name_row = info[0]

    if not name_row:
        return _parse_plain(text, source)

    channels = []
    for i, nm in enumerate(name_row):
        unit = unit_row[i] if unit_row and i < len(unit_row) else ""
        channels.append(LogChannel(raw_name=nm, canonical=map_channel(nm), unit=unit))

    data_rows = [r for r in rows[cd + 1:] if r]
    for row in data_rows:
        for i, ch in enumerate(channels):
            ch.values.append(_to_float(row[i]) if i < len(row) else None)

    return Log(channels=channels, n_samples=len(data_rows), source=source)


# --------------------------------------------------------------------------- #
# The overlay: bin log samples into a table's cells
# --------------------------------------------------------------------------- #
@dataclass
class CellBin:
    count: int = 0
    _sum: float = 0.0

    def add(self, v: float) -> None:
        self.count += 1
        self._sum += v

    @property
    def mean(self) -> Optional[float]:
        return (self._sum / self.count) if self.count else None


@dataclass
class Overlay:
    """Result of binning a log against a table."""
    n_rows: int
    n_cols: int
    bins: list[list[CellBin]]
    total_binned: int
    value_channel: Optional[str]      # canonical key averaged into cells

    def count_grid(self) -> list[list[int]]:
        return [[b.count for b in row] for row in self.bins]

    def mean_grid(self) -> list[list[Optional[float]]]:
        return [[b.mean for b in row] for row in self.bins]

    def hottest_cell(self) -> Optional[tuple[int, int, int]]:
        best = None
        for r, row in enumerate(self.bins):
            for c, b in enumerate(row):
                if best is None or b.count > best[2]:
                    best = (r, c, b.count)
        return best


def bin_log_to_table(
    log: Log,
    table: Table,
    x_channel: str,
    y_channel: Optional[str] = None,
    value_channel: Optional[str] = None,
) -> Overlay:
    """
    Bin every sample into table cells using the sample's x_channel (and
    y_channel for 2-D tables) against the table's axes. If value_channel is
    given, accumulate its mean per cell; otherwise the overlay is a pure
    operation-count histogram.
    """
    xs = log.series(x_channel)
    ys = log.series(y_channel) if y_channel else None
    vs = log.series(value_channel) if value_channel else None

    n_rows = table.n_rows
    n_cols = table.n_cols
    bins = [[CellBin() for _ in range(n_cols)] for _ in range(n_rows)]

    total = 0
    n = log.n_samples
    for i in range(n):
        x = xs[i] if i < len(xs) else None
        if x is None:
            continue
        c = table.x_axis.index_of(x)
        if c is None:
            continue
        if table.y_axis:
            if ys is None:
                continue
            y = ys[i] if i < len(ys) else None
            if y is None:
                continue
            r = table.y_axis.index_of(y)
            if r is None:
                continue
        else:
            r = 0
        # accumulate
        val = None
        if vs is not None and i < len(vs):
            val = vs[i]
        bins[r][c].add(val if val is not None else 0.0)
        if val is None and value_channel is not None:
            # don't count a sample toward a value-mean if the value is missing
            bins[r][c].count -= 1
            bins[r][c]._sum -= 0.0
        else:
            total += 1

    return Overlay(
        n_rows=n_rows, n_cols=n_cols, bins=bins,
        total_binned=total, value_channel=value_channel,
    )


# --------------------------------------------------------------------------- #
# Regime analysis (the 8.8.26 baseline logic → wot_analyzer)
# --------------------------------------------------------------------------- #
@dataclass
class RegimeStats:
    name: str
    n: int
    stft_mean: Optional[float] = None
    ltft_mean: Optional[float] = None
    combined_trim_mean: Optional[float] = None


@dataclass
class LogReport:
    n_samples: int
    channels_present: list[str]
    duration_s: Optional[float]
    regimes: list[RegimeStats]
    knock_events: list[dict]
    max_knock_retard: Optional[float]
    fuel_pressure_min: Optional[float]
    fuel_pressure_sag: bool
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Long-format sidecar (<stem>.long.csv, written by recorder.DriveRecorder):
# one line per value with that value's OWN request time. A tiered GT log has
# blanks in the wide CSV for not-due channels, and every wide row's single
# time is only its cycle midpoint; the sidecar is the precise record.
# --------------------------------------------------------------------------- #
LONG_HEADER = ("Time (s)", "Channel", "Value")


def parse_long_csv(text: str) -> dict[str, list[tuple[float, float]]]:
    """Sidecar text -> {canonical key: [(t, value), ...] sorted by t}.
    Lines that do not parse are skipped (never guessed); a file whose header
    is not the sidecar's raises ValueError. The recorder ends every line
    with a newline, so a final line WITHOUT one was cut off mid-write (a
    crash): '0.2500,rpm,80' from '0.2500,rpm,800' would parse as a valid
    but wrong value, so that line is dropped."""
    if text and not text.endswith(("\n", "\r")):
        cut = max(text.rfind("\n"), text.rfind("\r"))
        text = text[:cut + 1] if cut >= 0 else ""
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or tuple(h.strip() for h in header[:3]) != LONG_HEADER:
        raise ValueError("not an OpenOBD long-format sidecar")
    out: dict[str, list[tuple[float, float]]] = {}
    for row in reader:
        if len(row) < 3:
            continue
        t, v = _to_float(row[0]), _to_float(row[2])
        key = row[1].strip()
        if t is None or v is None or not key:
            continue
        out.setdefault(key, []).append((t, v))
    for series in out.values():
        series.sort(key=lambda tv: tv[0])
    return out


def attach_long(log: "Log", text: str) -> "Log":
    """Attach a sidecar's per-channel timed series to a parsed wide log;
    analyze_log / detect_shift_points then use each channel's own times."""
    log.timed = parse_long_csv(text)
    return log


class _Timed:
    """Nearest-in-time lookup over one channel's (t, value) readings."""

    def __init__(self, series: list[tuple[float, float]]):
        self.t = [p[0] for p in series]
        self.v = [p[1] for p in series]

    def __bool__(self):
        return bool(self.t)

    def at(self, t: float, tol: float) -> Optional[float]:
        """The reading nearest t, if within tol seconds; else None. Analysis
        pairing only: nothing here is written back as a sample."""
        if not self.t:
            return None
        i = bisect.bisect_left(self.t, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.t):
                d = abs(self.t[j] - t)
                if d <= tol and (best is None or d < best[0]):
                    best = (d, self.v[j])
        return None if best is None else best[1]


def _pair_tolerance(rpm: "_Timed") -> float:
    """Pairing window: one rpm sampling period (median spacing), at least
    0.25 s, so a value is classified by the rpm/pedal read in the same or an
    adjacent poll cycle."""
    gaps = sorted(b - a for a, b in zip(rpm.t, rpm.t[1:]) if b > a)
    med = gaps[len(gaps) // 2] if gaps else 0.0
    return max(0.25, med)


def _mean(vals: Iterable[Optional[float]]) -> Optional[float]:
    xs = [v for v in vals if v is not None]
    return sum(xs) / len(xs) if xs else None


def _regime(r: Optional[float], a: Optional[float]) -> Optional[str]:
    if r is None:
        return None
    if r < 900 and (a is None or a < 5):
        return "idle"
    if a is not None and a >= 75:
        return "PE (WOT/high load)"
    if a is not None and a < 25:
        return "cruise"
    return "part-throttle"


def _timed_regimes(log: Log) -> Optional[list]:
    """Regime trims from the sidecar's per-channel times, or None when the
    log has no usable sidecar. Each rpm reading is a regime point (pedal
    paired by time); each STFT / LTFT reading is classified by the rpm and
    pedal nearest ITS OWN time, so a MEDIUM-tier LTFT is never averaged
    against the wrong cycle's operating point."""
    td = log.timed or {}
    rpm = _Timed(td.get("rpm", []))
    if not rpm:
        return None
    app = _Timed(td.get("app", []) or td.get("tps", []))
    tol = _pair_tolerance(rpm)

    def regime_at(t: float) -> Optional[str]:
        return _regime(rpm.at(t, tol), app.at(t, tol) if app else None)

    counts: dict[str, int] = {}
    for t, r in zip(rpm.t, rpm.v):
        rg = _regime(r, app.at(t, tol) if app else None)
        if rg:
            counts[rg] = counts.get(rg, 0) + 1
    trims: dict[str, dict[str, list[float]]] = {}
    for key in ("stft", "ltft"):
        for t, v in td.get(key, []):
            rg = regime_at(t)
            if rg:
                trims.setdefault(rg, {}).setdefault(key, []).append(v)
    out = []
    for name, cnt in counts.items():
        tr = trims.get(name, {})
        s = _mean(tr.get("stft", [])) if tr.get("stft") else None
        l = _mean(tr.get("ltft", [])) if tr.get("ltft") else None
        comb = None if s is None and l is None else (s or 0.0) + (l or 0.0)
        out.append(RegimeStats(name, cnt, s, l, comb))
    return out


def analyze_log(log: Log) -> LogReport:
    notes: list[str] = []
    rpm = log.series("rpm")
    app = log.series("app") or log.series("tps")
    load = log.series("load")
    stft = log.series("stft")
    ltft = log.series("ltft")
    kr = log.series("knock_retard")
    fp = log.series("fuel_press")
    spark = log.series("spark")
    t = log.series("time")

    n = log.n_samples
    duration = None
    if t:
        tv = [x for x in t if x is not None]
        if len(tv) >= 2:
            duration = max(tv) - min(tv)

    # Regime classification per sample.
    def regime_of(i: int) -> Optional[str]:
        r = rpm[i] if i < len(rpm) else None
        a = app[i] if app and i < len(app) else None
        if r is None:
            return None
        if r < 900 and (a is None or a < 5):
            return "idle"
        if a is not None and a >= 75:
            return "PE (WOT/high load)"
        if a is not None and a < 25:
            return "cruise"
        return "part-throttle"

    regimes: list[RegimeStats] = []
    timed = _timed_regimes(log)
    if timed is not None:
        regimes = timed
    else:
        buckets: dict[str, list[int]] = {}
        for i in range(n):
            rg = regime_of(i)
            if rg:
                buckets.setdefault(rg, []).append(i)
        for name, idxs in buckets.items():
            s = _mean(stft[i] for i in idxs if i < len(stft)) if stft else None
            l = _mean(ltft[i] for i in idxs if i < len(ltft)) if ltft else None
            comb = None
            if s is not None or l is not None:
                comb = (s or 0.0) + (l or 0.0)
            regimes.append(RegimeStats(name, len(idxs), s, l, comb))
    regimes.sort(key=lambda r: -r.n)

    # Knock events. Prefer a real KR channel; else detect a timing-collapse
    # signature (sharp spark drop) when KR isn't logged.
    knock_events: list[dict] = []
    max_kr = None
    if kr:
        krv = [v for v in kr if v is not None]
        max_kr = max(krv) if krv else None
        for i, v in enumerate(kr):
            if v is not None and v >= 1.0:
                knock_events.append({
                    "sample": i,
                    "time_s": t[i] if t and i < len(t) else None,
                    "retard_deg": v,
                    "rpm": rpm[i] if i < len(rpm) else None,
                })
    elif spark:
        notes.append("No knock-retard channel logged — add it before trusting "
                     "spark decisions. Timing-collapse heuristic used instead.")
        for i in range(1, len(spark)):
            a, b = spark[i - 1], spark[i]
            if a is not None and b is not None and (a - b) >= 4.0:
                knock_events.append({
                    "sample": i,
                    "time_s": t[i] if t and i < len(t) else None,
                    "spark_drop_deg": round(a - b, 1),
                    "rpm": rpm[i] if i < len(rpm) else None,
                    "inferred": True,
                })

    fp_min = None
    fp_sag = False
    if fp:
        fpv = [v for v in fp if v is not None]
        if fpv:
            fp_min = min(fpv)
            base = _mean(fpv)
            if base and fp_min < 0.85 * base:
                fp_sag = True
                notes.append(f"Fuel pressure sagged to {fp_min:.0f} "
                             f"(~{100*fp_min/base:.0f}% of mean) — watch pump/PE.")

    # Rich/lean systemic note.
    if regimes:
        cruise = next((r for r in regimes if r.name == "cruise"), None)
        if cruise and cruise.combined_trim_mean is not None:
            if cruise.combined_trim_mean <= -8:
                notes.append(f"Cruise trims average {cruise.combined_trim_mean:+.1f}%"
                             " (rich) — possible systemic MAF over-read.")
            elif cruise.combined_trim_mean >= 8:
                notes.append(f"Cruise trims average {cruise.combined_trim_mean:+.1f}%"
                             " (lean) — MAF under-read / small leak.")

    has_pe = (any(r.name == "PE (WOT/high load)" for r in regimes)
              if timed is not None else
              any(regime_of(i) == "PE (WOT/high load)" for i in range(n)))
    if not has_pe:
        notes.append("No WOT/PE samples in this log — can't validate shift points "
                     "or PE fueling. Capture a full-throttle pull with KR logged.")

    return LogReport(
        n_samples=n,
        channels_present=sorted(set(log.canonical_keys())),
        duration_s=duration,
        regimes=regimes,
        knock_events=knock_events,
        max_knock_retard=max_kr,
        fuel_pressure_min=fp_min,
        fuel_pressure_sag=fp_sag,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Observed shift-point detection — the meaningful "log view" for the WOT shift
# setpoint tables (validates #24: confirm 1-2/2-3 land near 5,400-5,500 RPM).
# --------------------------------------------------------------------------- #
@dataclass
class ShiftEvent:
    from_gear: Optional[int]
    to_gear: Optional[int]
    time_s: Optional[float]
    rpm: Optional[float]        # engine speed just before the shift
    vss: Optional[float]        # vehicle speed at the shift
    wot: bool                   # pedal/throttle high at the time
    inferred: bool = False      # detected from RPM drop, no gear channel


def _timed_shifts(log: Log, wot_threshold: float) -> Optional[list]:
    """Shift detection on the sidecar's per-channel times, or None without
    one. Same rules as the wide path (gear increments, else an RPM drop of
    400+ with VSS not falling), but consecutive readings are the channel's
    OWN readings at their own times, and vss / pedal are paired by time."""
    td = log.timed or {}
    rpm = _Timed(td.get("rpm", []))
    if not rpm:
        return None
    vss = _Timed(td.get("vss", []))
    app = _Timed(td.get("app", []) or td.get("tps", []))
    gear = _Timed(td.get("gear", []))
    tol = _pair_tolerance(rpm)

    def wot(t: float) -> bool:
        a = app.at(t, tol) if app else None
        return a is not None and a >= wot_threshold

    events: list[ShiftEvent] = []
    if gear:
        last, last_t = None, None
        for t, g in zip(gear.t, gear.v):
            g = round(g)
            if last is not None and g > last:
                events.append(ShiftEvent(
                    from_gear=last, to_gear=g, time_s=last_t,
                    rpm=rpm.at(last_t, tol),
                    vss=vss.at(last_t, tol) if vss else None,
                    wot=wot(last_t)))
            last, last_t = g, t
        return events
    for i in range(1, len(rpm.t)):
        t0, t1 = rpm.t[i - 1], rpm.t[i]
        r0, r1 = rpm.v[i - 1], rpm.v[i]
        v0 = vss.at(t0, tol) if vss else None
        v1 = vss.at(t1, tol) if vss else None
        if (r0 - r1) >= 400 and (v0 is None or v1 is None or v1 >= v0 - 1):
            events.append(ShiftEvent(
                from_gear=None, to_gear=None, time_s=t1, rpm=r0, vss=v0,
                wot=wot(t0), inferred=True))
    return events


def detect_shift_points(log: Log, wot_threshold: float = 70.0) -> list[ShiftEvent]:
    """
    Return upshift events. If a 'gear' channel is present, detect increments
    directly (most reliable). Otherwise infer upshifts from a sharp RPM drop
    while VSS keeps climbing (auto upshift signature).
    """
    timed = _timed_shifts(log, wot_threshold)
    if timed is not None:
        return timed
    rpm = log.series("rpm")
    vss = log.series("vss")
    gear = log.series("gear")
    app = log.series("app") or log.series("tps")
    t = log.series("time")
    n = log.n_samples
    events: list[ShiftEvent] = []

    def is_wot(i: int) -> bool:
        a = app[i] if app and i < len(app) else None
        return a is not None and a >= wot_threshold

    if gear and any(v is not None for v in gear):
        last = None
        for i in range(n):
            g = gear[i] if i < len(gear) else None
            if g is None:
                continue
            g = round(g)
            if last is not None and g > last:
                j = max(0, i - 1)
                events.append(ShiftEvent(
                    from_gear=last, to_gear=g,
                    time_s=t[j] if t and j < len(t) else None,
                    rpm=rpm[j] if j < len(rpm) else None,
                    vss=vss[j] if vss and j < len(vss) else None,
                    wot=is_wot(j),
                ))
            last = g
        return events

    # Inferred path: RPM drops >= 400 while VSS non-decreasing.
    for i in range(2, n):
        r0 = rpm[i - 1] if i - 1 < len(rpm) else None
        r1 = rpm[i] if i < len(rpm) else None
        v0 = vss[i - 1] if vss and i - 1 < len(vss) else None
        v1 = vss[i] if vss and i < len(vss) else None
        if None in (r0, r1):
            continue
        if (r0 - r1) >= 400 and (v0 is None or v1 is None or v1 >= v0 - 1):
            events.append(ShiftEvent(
                from_gear=None, to_gear=None,
                time_s=t[i] if t and i < len(t) else None,
                rpm=r0, vss=v0,
                wot=is_wot(i - 1), inferred=True,
            ))
    return events
