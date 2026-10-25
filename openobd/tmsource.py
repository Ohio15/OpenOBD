"""
tmsource — feed the Dashboard's gauge cluster from truck-mcp drive-log sessions.

This is the adapter that lets a *.tmsession.db drive the same DataSource seam
the GT link and CSV replay use, so the gauges never learn a second data path.
Two shapes, chosen by whether the drive is still being written:

  * An ARCHIVED session becomes a synthesized `logbin.Log` (mapped channels on a
    unified forward-filled timeline) played through the existing LogReplaySource
    — the Dashboard's transport bar (seek/pause/speed) works unchanged.

  * A LIVE session becomes a TmLiveSource: `latest()` follows channel_latest at
    the store's own cadence (reads are throttled to ~1 Hz internally; the
    Dashboard may call at 10 Hz). It also exposes `channel_states()` so the
    cluster can render tmstore's freshness vocabulary — a carried, stale or
    archived value that renders like a live measurement is the tool lying about
    the truck, exactly the contract tmstore.display_state exists to keep.

Mapping is explicit and unit-checked: a store channel reaches a gauge ONLY if
its name is in TM_CHANNEL_MAP *and* its declared unit has a registered
conversion to the gauge's display unit. A channel with an empty or unknown unit
is EXCLUDED (reported, not guessed) — e.g. trans_fluid_temp, whose E38 DID is
still unverified and carries no unit in the store, must not move the °F Trans
bar. Values are converted (°C→°F, km/h→mph) at the seam; the gauges keep their
US display units.

Read-only by construction: everything goes through tmstore.TmSessionReader
(query_only + authorizer). Nothing here can touch the serial port.
"""
from __future__ import annotations

import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import tmstore
from .logbin import Log, LogChannel
from .transport import DataSource, LogReplaySource, Sample

# The failures a store read may legitimately hit (WAL checkpoint, file lock,
# file gone). Anything else is a bug in THIS code and must propagate — the
# mistake of catching Exception here is what turns a programming error into a
# silent "transient store failure".
_READ_ERRORS = (sqlite3.Error, tmstore.TmSessionError, OSError)

# Store channel -> (canonical gauge key, gauge display unit tag).
# The unit tag is matched against the channel's DECLARED unit via _CONVERTERS;
# no entry for the declared unit means the channel is excluded, never guessed.
TM_CHANNEL_MAP: dict[str, tuple[str, str]] = {
    "rpm":             ("rpm",     "rpm"),
    "vehicle_speed":   ("vss",     "mph"),
    "coolant_temp":    ("ect",     "f"),
    "intake_air_temp": ("iat",     "f"),
    "throttle_pos":    ("tps",     "%"),
    "control_voltage": ("voltage", "v"),
    "engine_load":     ("load",    "%"),
    "trans_fluid_temp": ("tft",    "f"),
    # Channels other truck-mcp sweep presets record (unit-gated like the rest).
    "maf":             ("maf",     "g/s"),
    "intake_map":      ("map",     "kpa"),
    "stft_bank1":      ("stft",    "%"),
    "ltft_bank1":      ("ltft",    "%"),
}


def _c_to_f(v: float) -> float:
    return v * 9.0 / 5.0 + 32.0


def _kmh_to_mph(v: float) -> float:
    return v * 0.621371


def _ident(v: float) -> float:
    return v


# (normalized declared unit, gauge unit tag) -> converter
_CONVERTERS: dict[tuple[str, str], Callable[[float], float]] = {
    ("c", "f"): _c_to_f,
    ("f", "f"): _ident,
    ("km/h", "mph"): _kmh_to_mph,
    ("kph", "mph"): _kmh_to_mph,
    ("mph", "mph"): _ident,
    ("%", "%"): _ident,
    ("v", "v"): _ident,
    ("rpm", "rpm"): _ident,
    ("g/s", "g/s"): _ident,
    ("kpa", "kpa"): _ident,
}


def _norm_unit(unit: Optional[str]) -> str:
    u = (unit or "").strip().lower()
    u = u.replace("°", "").replace("deg", "").strip()
    return u


@dataclass
class ResolvedMap:
    """name -> (canonical, converter) for feedable channels, plus why the rest
    were left out — shown to the operator, never silently dropped."""
    feed: dict[str, tuple[str, Callable[[float], float]]] = field(default_factory=dict)
    excluded: list[tuple[str, str]] = field(default_factory=list)   # (name, reason)


def resolve_channels(channels: list[dict]) -> ResolvedMap:
    out = ResolvedMap()
    for ch in channels:
        name = ch.get("name")
        if name not in TM_CHANNEL_MAP:
            continue   # sidecars / unmapped channels are Live Data's job
        if (ch.get("kind") or "numeric") != "numeric":
            out.excluded.append((name, "not numeric"))
            continue
        # An unverified decode must never move a gauge, independently of unit —
        # relying on truck-mcp not writing a unit for candidates would break
        # the day it starts to (the DID 1940 trans-temp guess is `candidate`).
        conf = (ch.get("confidence") or "").strip().lower()
        if conf and conf != "verified":
            out.excluded.append((name, f"confidence {conf!r} — not verified"))
            continue
        canonical, gauge_unit = TM_CHANNEL_MAP[name]
        declared = _norm_unit(ch.get("unit"))
        conv = _CONVERTERS.get((declared, gauge_unit))
        if conv is None:
            reason = ("no unit declared" if not declared
                      else f"unit {ch.get('unit')!r} not convertible to {gauge_unit}")
            out.excluded.append((name, reason))
            continue
        out.feed[name] = (canonical, conv)
    return out


# --------------------------------------------------------------------------- #
# Archived drive -> synthesized Log for the existing LogReplaySource
# --------------------------------------------------------------------------- #
# Ceiling on raw sample rows build_replay_log will load. The build runs on the
# GUI thread; past this it is refused with a clear message rather than freezing
# the app for tens of seconds (a 1.4M-row session measured ~30 s / ~780 MB).
MAX_REPLAY_SAMPLES = 200_000


def build_replay_log(path: str | Path) -> tuple[Log, ResolvedMap]:
    """Read an archived session into a logbin.Log on a unified timeline.

    Only FRESH samples are read (carried rows are the store's own forward-fill;
    re-deriving them here keeps one source of truth) — but a fresh row whose
    quality is not OK, or that carries no value, BREAKS the forward-fill for its
    channel: a module fault window must replay as "—", never as the last good
    value frozen in place. Channels merge onto the union of their timestamps;
    each channel forward-fills between its own good samples and is None before
    its first. Timestamps that don't parse are skipped rather than guessed.
    """
    try:
        with tmstore.TmSessionReader(path) as reader:
            resolved = resolve_channels(reader.channels())
            if not resolved.feed:
                raise tmstore.TmSessionError(
                    "no session channel maps onto a gauge (see Live Data for "
                    "the raw view)")
            # Count exactly the rows the series read below would touch — a
            # session dominated by unmapped sidecar rows must not be refused
            # on rows the replay would never load.
            n_rows = reader.sample_count(list(resolved.feed), fresh_only=True)
            if n_rows > MAX_REPLAY_SAMPLES:
                raise tmstore.TmSessionError(
                    f"session has {n_rows} gauge samples — too large to "
                    f"replay (limit {MAX_REPLAY_SAMPLES}); use truck-mcp's "
                    "analysis tools for drives this long")
            series = reader.series(list(resolved.feed), fresh_only=True)
    except sqlite3.Error as exc:
        # Same surface as every other store fault in this module — never a
        # raw SQL error reaching the UI.
        raise tmstore.TmSessionError(f"store read failed: {exc}") from exc

    # (epoch seconds, name, converted value | None=fill break), time-ordered
    points: list[tuple[float, str, Optional[float]]] = []
    for name, rows in series.items():
        canonical, conv = resolved.feed[name]
        for row in rows:
            ts = tmstore._parse_ts(row.get("ts_utc"))
            if ts is None:
                continue
            v = row.get("value_num")
            if row.get("quality") != tmstore.QUALITY_OK or v is None:
                points.append((ts.timestamp(), name, None))   # break the fill
                continue
            try:
                points.append((ts.timestamp(), name, conv(float(v))))
            except (TypeError, ValueError):
                points.append((ts.timestamp(), name, None))
    if not any(v is not None for _t, _n, v in points):
        raise tmstore.TmSessionError("session has no usable fresh samples")
    points.sort(key=lambda p: p[0])

    times: list[float] = []
    columns: dict[str, list[Optional[float]]] = {n: [] for n in resolved.feed}
    last: dict[str, Optional[float]] = {n: None for n in resolved.feed}
    i = 0
    while i < len(points):
        t = points[i][0]
        while i < len(points) and points[i][0] == t:
            _, name, v = points[i]
            last[name] = v
            i += 1
        times.append(t)
        for name in columns:
            columns[name].append(last[name])

    # A channel declared in the session but never actually sampled must not
    # surface as a fed gauge — drop it and say why, so the gauge stays dimmed
    # instead of rendering bright with a permanent "—".
    for name in [n for n, col in columns.items()
                 if not any(v is not None for v in col)]:
        resolved.excluded.append((name, "no samples in this drive"))
        del resolved.feed[name]
        del columns[name]

    t0 = times[0]
    chans = [LogChannel("time", "time", "s", [t - t0 for t in times])]
    for name, (canonical, _conv) in resolved.feed.items():
        chans.append(LogChannel(name, canonical, "", columns[name]))
    log = Log(channels=chans, n_samples=len(times),
              source=f"tmsession:{Path(path).name}")
    return log, resolved


class TmReplaySource(LogReplaySource):
    """LogReplaySource over a synthesized session log that KEEPS the archive
    vocabulary: every channel reports state 'archive', so the cluster badges
    the whole replay as an archived drive instead of styling it live."""

    is_store_record = True   # the session file is already the record

    def __init__(self, log: Log, speed: float = 1.0):
        super().__init__(log, speed=speed)
        self._archive_states = {k: "archive" for k in self.channels()}

    def channel_states(self) -> dict[str, str]:
        return dict(self._archive_states)


# --------------------------------------------------------------------------- #
# Live drive -> polling DataSource with a freshness vocabulary
# --------------------------------------------------------------------------- #
# How often the store is actually re-read, regardless of how often the
# Dashboard's paint timer calls latest(). Matches Live Data's cadence — the
# fast tier commits ~1 Hz, so reading faster only burns the GUI thread.
READ_INTERVAL_S = 1.0
# Consecutive read failures before the source stops showing values and flips
# every channel to an explicit failed state (mirrors LiveDataPage).
FAIL_LIMIT = 2


class TmLiveSource(DataSource):
    """Follows a live *.tmsession.db through tmstore's read-only reader.

    latest() is non-blocking in spirit: it re-reads the SQLite file at most
    every READ_INTERVAL_S (a channel_latest read is a few ms — the same work
    Live Data already does inline at 1 Hz). channel_states() reports each
    gauge key's tmstore display state so the cluster can say what is live,
    carried, stale, or failed instead of rendering everything as a live needle.
    """

    is_store_record = True   # truck-mcp is writing the record itself

    # Short SQLite busy wait: this reader is polled from the GUI thread, so a
    # writer's WAL checkpoint must cost a failed read (rendered honestly as
    # stale), never a multi-second UI freeze.
    BUSY_TIMEOUT_S = 0.5

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._reader = tmstore.TmSessionReader(self.path,
                                               timeout=self.BUSY_TIMEOUT_S)
        try:
            meta = self._reader.metadata()
            chans = self._reader.channels()
        except Exception:
            self._reader.close()
            raise
        self._meta = meta
        self._channel_meta = {c["name"]: c for c in chans}
        self._resolved = resolve_channels(chans)
        if not self._resolved.feed:
            self._reader.close()
            raise tmstore.TmSessionError(
                "no session channel maps onto a gauge (see Live Data for the "
                "raw view)")
        self._archived = bool(meta.get("ended_utc"))
        self._values: dict[str, float] = {}
        self._states: dict[str, str] = {}
        self._latest_sample: Optional[Sample] = None
        self._queue: deque = deque(maxlen=8192)
        self._last_read = 0.0
        self._last_ts: Optional[str] = None
        self._fail_streak = 0
        self._t0: Optional[float] = None
        self._started = False
        self._closed = False
        self.device = f"truck-mcp session {meta.get('label') or self.path.name}"

    @property
    def excluded(self) -> list[tuple[str, str]]:
        return list(self._resolved.excluded)

    def channels(self) -> list[str]:
        return [c for c, _f in self._resolved.feed.values()]

    def start(self) -> None:
        if self._closed:
            # stop() closed the reader; a restart must reopen it, not serve the
            # pre-stop values through a dead handle.
            self._reader = tmstore.TmSessionReader(self.path,
                                                   timeout=self.BUSY_TIMEOUT_S)
            self._closed = False
            self._fail_streak = 0
        self._started = True
        self._t0 = time.monotonic()
        self._last_read = 0.0
        self._read()

    def stop(self) -> None:
        self._started = False
        self._closed = True
        try:
            self._reader.close()
        except sqlite3.Error:
            pass

    def latest(self) -> Optional[Sample]:
        if not self._started:
            return None
        now = time.monotonic()
        if now - self._last_read >= READ_INTERVAL_S:
            self._read(now=now)
        return self._latest_sample

    def channel_states(self) -> dict[str, str]:
        """canonical gauge key -> tmstore display state (plus 'failed')."""
        return dict(self._states)

    def drain(self) -> list[Sample]:
        out = []
        while True:
            try:
                out.append(self._queue.popleft())
            except IndexError:
                return out

    # -- internal ----------------------------------------------------------- #
    def _read(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self._last_read = now
        try:
            raw = self._reader.latest(list(self._resolved.feed))
            newest_ts = self._reader.latest_ts()
            if not self._archived and self._reader.ended():
                self._archived = True
        except _READ_ERRORS:
            self._fail_streak += 1
            if self._fail_streak >= FAIL_LIMIT:
                # The read itself failed — nothing may keep rendering as data.
                self._states = {c: "failed"
                                for c, _f in self._resolved.feed.values()}
                self._latest_sample = None
            else:
                # Even ONE failed read means the view is not advancing: demote
                # anything live so a frozen value can't keep a fresh badge for
                # the seconds until the next attempt resolves it. If no states
                # exist yet (start()'s own read failed), report every channel
                # stale rather than an empty dict the cluster reads as "plain".
                if self._states:
                    self._states = {c: ("stale" if s == "fresh" else s)
                                    for c, s in self._states.items()}
                else:
                    self._states = {c: "stale"
                                    for c, _f in self._resolved.feed.values()}
            return
        self._fail_streak = 0

        session_stale = False
        if not self._archived and newest_ts:
            age = tmstore.sample_age_s({"ts_utc": newest_ts})
            session_stale = (age is not None
                             and age > tmstore.STALE_SESSION_LIMIT_S)

        values: dict[str, float] = {}
        states: dict[str, str] = {}
        for name, (canonical, conv) in self._resolved.feed.items():
            sample = raw.get(name)
            # display_state is the ONE classifier — reuse it for the state so
            # this source can never invent a freshness tmstore wouldn't.
            try:
                ds = tmstore.display_state(
                    sample, self._channel_meta.get(name),
                    archived=self._archived, session_stale=session_stale)
            except Exception:
                states[canonical] = "badvalue"
                continue
            states[canonical] = ds.state
            if ds.state in ("fresh", "stale", "archive", "carried"):
                v = sample.get("value_num") if sample else None
                if v is not None:
                    try:
                        values[canonical] = conv(float(v))
                    except (TypeError, ValueError):
                        states[canonical] = "badvalue"

        self._values = values
        self._states = states
        t = now - (self._t0 or now)
        s = Sample(t=t, values=values)
        self._latest_sample = s
        # New committed store state -> one recorder sample (dedup by newest_ts
        # so the 10 Hz caller doesn't multiply the store's 1 Hz cadence).
        if newest_ts != self._last_ts:
            self._last_ts = newest_ts
            self._queue.append(s)
