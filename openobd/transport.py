"""
transport — the seam the live dashboard reads from.

The dashboard never talks to hardware directly. It reads canonical channel
samples from a DataSource. Today we ship a LogReplaySource (plays a loaded log
back in time order) so the dashboard is fully usable before the GT is wired.
When gt.py lands, a GtDataSource implementing the same three methods drops in
with zero dashboard changes — the same seam `gt.py` implements for the CLI
(open/close/command/request_raw) surfaces canonical samples here.
"""
from __future__ import annotations

import bisect
import logging
import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

from .logbin import Log, time_axis
from .pedal import PedalCal, PedalLearner
from .pollplan import PollPlan, fresh_window_s

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reading:
    """One channel value with the exchange that produced it.

    sent / received: when the request was written and its reply read in
                     full. ObdxGt stamps on time.monotonic(); a DataSource
                     rebases both onto its own clock (seconds since connect).
    request:         sequence number of that request on the link. Values
                     decoded from the same response share it."""
    key: str
    value: float
    sent: float
    received: float
    request: int

    @property
    def t(self) -> float:
        """Best estimate of when the module sampled the value: the midpoint
        of its request/response exchange."""
        return (self.sent + self.received) / 2


@dataclass
class Sample:
    """A single timestamped snapshot: canonical_key -> value.

    readings, when present, carries the per-request truth behind `values`:
    each value with its own request's exchange times. A sequential GT sweep
    spans ~1.4 s, so one time per row cannot date every value in it
    (2026-10-25, brain 8d92fae5). For such a sample `t` is the sweep
    midpoint and sweep_start()/sweep_end() bound the row's skew.

    derived, when present, maps each DERIVED key in `values` (computed from
    other channels, not requested from a module) to the key it was computed
    from: {"pedal": "app"}. A derived value has no Reading of its own; it is
    dated by its source key's reading."""
    t: float
    values: dict[str, float]
    readings: Optional[tuple[Reading, ...]] = None
    derived: Optional[dict[str, str]] = None

    def sweep_start(self) -> Optional[float]:
        return min(r.sent for r in self.readings) if self.readings else None

    def sweep_end(self) -> Optional[float]:
        return max(r.received for r in self.readings) if self.readings else None


class DataSource:
    """Abstract live/replay source of canonical channel samples."""

    #: True only for sources that replay/follow a store which is ALREADY the
    #: durable record (truck-mcp drive-log sessions). The Dashboard refuses to
    #: re-record those. Declared, never inferred from other capabilities: the
    #: old duck-typed check (hasattr channel_states) also matched the GT once
    #: it grew freshness states, which silently disabled GT recording.
    is_store_record = False

    def channels(self) -> list[str]:
        raise NotImplementedError

    def recordable_channels(self) -> list[str]:
        """Every key this source can EVER produce — the recorder's columns.
        Defaults to channels(); a live source whose channels() is a snapshot
        of what answered so far overrides it."""
        return self.channels()

    def latest(self) -> Optional[Sample]:
        """Non-blocking: the most recent sample, or None if not started."""
        raise NotImplementedError

    def drain(self) -> list[Sample]:
        """All samples produced since the last drain() call, oldest first.
        Lossless (up to an internal bound) — the recorder reads this, so it
        never misses samples between UI ticks nor sees duplicates."""
        return []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def clock(self) -> Optional[float]:
        """The source's current time on its Sample clock, for stamping a
        user event (a recording marker) between samples; None for a source
        without a live clock (the recorder then uses its last sample)."""
        return None


class LogReplaySource(DataSource):
    """
    Replays a parsed Log as if it were live, with transport controls:
    pause()/resume(), set_speed(), seek(t), position()/duration(). Elapsed
    log-time accumulates in _elapsed_base; while playing, wall-clock since the
    last anchor (scaled by speed) is added on top, so pause/speed changes and
    seeks compose without drift.
    """

    def __init__(self, log: Log, speed: float = 1.0, fallback_dt: float = 0.1):
        self.log = log
        self.speed = speed
        self._keys = [c.canonical for c in log.channels if c.canonical]
        self._times = time_axis(log, fallback_dt)
        self._cursor = 0
        self._drained = -1  # last sample index handed out by drain()
        self._started = False
        self.playing = False
        self._elapsed_base = 0.0
        self._wall_anchor: Optional[float] = None

    def channels(self) -> list[str]:
        return list(self._keys)

    # -- transport ---------------------------------------------------------- #
    def duration(self) -> float:
        return self._times[-1] if self._times else 0.0

    def _elapsed(self) -> float:
        e = self._elapsed_base
        if self.playing and self._wall_anchor is not None:
            e += (time.monotonic() - self._wall_anchor) * self.speed
        return e

    def position(self) -> float:
        return min(self._elapsed(), self.duration())

    def start(self) -> None:
        self._started = True
        self.playing = True
        self._elapsed_base = 0.0
        self._wall_anchor = time.monotonic()
        self._cursor = 0
        self._drained = -1

    def stop(self) -> None:
        self.pause()

    def pause(self) -> None:
        self._elapsed_base = self._elapsed()
        self.playing = False
        self._wall_anchor = None

    def resume(self) -> None:
        if self._started and not self.playing:
            self._wall_anchor = time.monotonic()
            self.playing = True

    def set_speed(self, speed: float) -> None:
        self._elapsed_base = self._elapsed()
        if self.playing:
            self._wall_anchor = time.monotonic()
        self.speed = speed

    def seek(self, t: float) -> None:
        self._started = True
        self._elapsed_base = max(0.0, min(t, self.duration()))
        if self.playing:
            self._wall_anchor = time.monotonic()
        i = bisect.bisect_right(self._times, self._elapsed_base) - 1
        self._cursor = max(0, i)
        # don't flood the recorder with every sample the seek skipped over
        self._drained = self._cursor

    # -- samples -------------------------------------------------------------- #
    def _sample_at(self, i: int) -> Sample:
        vals: dict[str, float] = {}
        for ch in self.log.channels:
            if ch.canonical and i < len(ch.values):
                v = ch.values[i]
                if v is not None:
                    vals[ch.canonical] = v
        return Sample(t=self._times[i] if i < len(self._times) else 0.0, values=vals)

    def latest(self) -> Optional[Sample]:
        if not self._started or self.log.n_samples == 0:
            return None
        elapsed = self._elapsed()
        i = self._cursor
        # advance cursor to the last sample whose log-time <= elapsed
        while i + 1 < len(self._times) and self._times[i + 1] <= elapsed:
            i += 1
        # loop the replay (only while actually playing)
        if (self.playing and i >= len(self._times) - 1
                and elapsed > self._times[-1]):
            self.start()
            i = 0
        self._cursor = i
        return self._sample_at(i)

    def drain(self) -> list[Sample]:
        # latest() advances the cursor (the dashboard tick calls it first);
        # hand out everything between the last drain and the cursor.
        if not self._started:
            return []
        out = [self._sample_at(i)
               for i in range(self._drained + 1, self._cursor + 1)]
        self._drained = self._cursor
        return out


class GtDataSource(DataSource):
    #: Consecutive failed polls before every gauge flips to "read failed" and
    #: latest() stops serving the last good sample (mirrors tmsource FAIL_LIMIT).
    FAIL_LIMIT = 2
    #: Consecutive failed polls on the current link before the poll thread
    #: closes and re-opens it. A USB unplug/replug (or a Windows re-enumeration
    #: onto a NEW COM number) leaves the old handle dead forever; before this
    #: the source never recovered and a recording silently stopped growing.
    RECONNECT_AFTER = 3
    #: Backoff between re-open attempts: first try immediately, then 1, 2, 4,
    #: ... seconds, capped. Interruptible by stop() and request_reconnect().
    RECONNECT_BASE_S = 1.0
    RECONNECT_MAX_S = 10.0

    """
    Live source backed by the OBDX Pro GT (openobd.gt.ObdxGt), an ELM327 v2.1
    interface over USB serial. start() opens the transport and spawns a daemon
    polling thread that fills a latest-sample slot; latest() is non-blocking so
    the dashboard's 100ms timer never stalls on serial IO. channels() reflects
    the canonical keys the PID table can surface (gauges pre-build), and after
    the first poll narrows to what the ECU actually answered;
    recordable_channels() keeps everything the link CAN produce.

    Link loss: after RECONNECT_AFTER failed polls the thread re-opens the GT,
    re-running VID/PID autodetect when no port was pinned, so a replug that
    comes back on a different COM number is found. Sample times keep counting
    from the original connect, so a recording shows the outage as a time gap.

    Timing: every value carries its own request's send/receive times (see
    Reading); a Sample's t is its cycle midpoint. The recorder zeroes time at
    its first recorded sample.

    Tiered polling (gt.ObdxGt.poll_timed / pollplan): one Sample per poll
    cycle holding ONLY the readings obtained in that cycle, so the recording
    shows a not-due channel as a blank cell. The live display must not
    flicker on those gaps, so the source also keeps each channel's last
    reading (channel_values / channel_ages / channel_states): a value stays
    "fresh" for twice its tier period, then reads "stale". That held value is
    for display only; it never enters a Sample or the recording.
    """

    #: Pause after a failed poll before the next try. Polling itself runs
    #: back to back (poll_interval 0): the link is the rate limit.
    FAIL_PAUSE_S = 0.05
    #: Smoothing for the measured cycle period (seconds between Samples).
    CYCLE_EMA_ALPHA = 0.3

    def __init__(self, port=None, poll_interval=0.0):
        from . import gt as _gt  # local import: pyserial only needed on this path
        self._gt = _gt.ObdxGt(port=port)
        self._keys = list(_gt.CANONICAL_KEYS)
        self._interval = poll_interval
        self._latest = None
        # Per-channel last reading on this source's clock, for the live
        # display only. Replaced (never mutated) by the poll thread, so a GUI
        # reader iterating its snapshot can't race an update.
        self._last_by_key: dict[str, Reading] = {}
        self._cycle_s: Optional[float] = None     # measured, EMA
        self._prev_t: Optional[float] = None
        # Divisor per channel and predicted cycle from the GT's poll plan,
        # captured on the poll thread (the plan may be rebuilt there).
        self._divisors: dict[str, int] = {}
        self._predicted_cycle_s: Optional[float] = None
        self._plan_seen = None
        self.plan_text: Optional[str] = None
        # Recorder queue, filled by the poll thread. Bounded so an idle
        # (non-recording) session can't grow it without limit; ~8k samples
        # is minutes of headroom at the real poll rate.
        self._queue: deque = deque(maxlen=8192)
        # Serializes serial IO between the poll thread and diagnostics calls
        # borrowing the link (run_exclusive).
        self._io_lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._t0 = None
        self.device = "OBDX Pro GT"
        self.port_name = port
        # Poll health. The loop used to swallow every exception, so a GT that
        # stopped answering (USB pulled, adapter wedged) left the Dashboard
        # frozen on its last values as if live (2026-10-01).
        self.fail_streak = 0
        self.last_error: str | None = None
        # Link recovery state (see RECONNECT_AFTER).
        self._explicit_port = port is not None
        self._fails_since_open = 0
        self._reconnect_attempts = 0     # attempts in the current outage
        self.reconnect_error: str | None = None
        self.reconnects = 0              # successful re-opens this session
        self._kick = threading.Event()   # cuts a backoff wait short
        # Pedal normalization (openobd.pedal): learned on the poll thread
        # from this connection's own data, seeded by the app from settings
        # (keyed by VIN) once the vehicle is identified.
        self.pedal = PedalLearner()
        self._pedal_lock = threading.Lock()
        # Vehicle identity (Mode 09) for THIS connection, read at the first
        # recording start (read_recording_context) and kept once the VIN
        # read; dropped on any re-open, which may be a different vehicle.
        self._identity: Optional[dict] = None

    def channels(self):
        return list(self._keys)

    #: Derived channels this source computes: key -> the channel it is
    #: computed from. Recorded and displayed, flagged as derived.
    DERIVED = {"pedal": "app"}

    def recordable_channels(self):
        possible = getattr(self._gt, "possible_keys", None)
        keys = set(self._keys)
        if possible is not None:
            keys |= set(possible())
        keys |= {d for d, src in self.DERIVED.items() if src in keys}
        return sorted(keys)

    def clock(self) -> Optional[float]:
        return self._now() if self._t0 is not None else None

    # -- pedal normalization ------------------------------------------------ #
    def pedal_calibration(self) -> PedalCal:
        """The calibration in force (a snapshot; ready or pending)."""
        return self.pedal.cal

    def seed_pedal_calibration(self, cal: PedalCal) -> None:
        """Merge a stored calibration (GUI thread) into this connection's
        learner; the poll thread uses the result from its next sample."""
        with self._pedal_lock:
            self.pedal.seed(cal)

    def _derive(self, s: Sample) -> None:
        """Poll thread: learn from this cycle's own values, then add the
        normalized pedal when (and only when) the calibration is ready and
        this cycle read APP D. Nothing is carried from another cycle."""
        v = s.values
        app_reading = next((r for r in s.readings or () if r.key == "app"),
                           None)
        t = app_reading.t if app_reading is not None else s.t
        with self._pedal_lock:
            self.pedal.observe(t, v.get("rpm"), v.get("vss"), v.get("app"),
                               v.get("load"), v.get("map"),
                               source="learned live")
            pedal = self.pedal.cal.normalize(v.get("app"))
        if pedal is not None:
            s.values["pedal"] = pedal
            s.derived = {"pedal": "app"}

    def failing(self) -> bool:
        return self.fail_streak >= self.FAIL_LIMIT

    def latest(self):
        # A failing source serves nothing: a stale sample must not render as
        # a live value. channel_states() says why.
        return None if self.failing() else self._latest

    def _now(self) -> float:
        return time.monotonic() - (self._t0 or 0.0)

    def channel_values(self) -> dict[str, float]:
        """Each channel's LAST READING (display only; it may be several
        cycles old for a MEDIUM/SLOW channel - channel_ages says how old)."""
        return {k: r.value for k, r in self._last_by_key.items()}

    def channel_ages(self, now: Optional[float] = None) -> dict[str, float]:
        """Seconds since each channel's last reading was sampled."""
        now = self._now() if now is None else now
        return {k: max(0.0, now - r.t) for k, r in self._last_by_key.items()}

    def fresh_window_s(self, key: str) -> Optional[float]:
        """Twice the channel's tier period, from the measured cycle time (the
        plan's prediction until a cycle has been measured)."""
        cycle = self._cycle_s or self._predicted_cycle_s
        return fresh_window_s(self._divisors.get(key, 1), cycle)

    def channel_states(self, now: Optional[float] = None) -> dict:
        """Freshness vocabulary the Dashboard already applies to tm sources:
        every channel "failed" while polls are failing; otherwise "fresh"
        when its last reading is within twice its tier period, "stale" when
        older, and None for a channel that has not answered yet."""
        last = self._last_by_key
        keys = set(self._keys) | set(last)
        if self.failing():
            return {k: "failed" for k in keys}
        now = self._now() if now is None else now
        out: dict = {}
        for k in keys:
            r = last.get(k)
            if r is None:
                out[k] = None
                continue
            window = self.fresh_window_s(k)
            out[k] = ("fresh" if window is None or now - r.t <= window
                      else "stale")
        return out

    def plan_summary(self) -> Optional[dict]:
        """The GT's poll plan as data (pollplan.PollPlan.summary), or None
        before the plan exists."""
        plan = self._plan_seen
        return plan.summary() if plan is not None else None

    def _capture_plan(self) -> None:
        """Poll thread: note the plan's tier divisors and prediction (the
        plan is rebuilt there on reconnect or a batching refusal)."""
        plan = getattr(self._gt, "_plan", None)
        if not isinstance(plan, PollPlan) or plan is self._plan_seen:
            return
        self._plan_seen = plan
        keys = set(plan.key_of.values()) | set(self._keys) | {"voltage"}
        self._divisors = {k: plan.divisor_of(k) for k in keys}
        for d, src in self.DERIVED.items():
            self._divisors[d] = plan.divisor_of(src)
        self._predicted_cycle_s = plan.mean_cycle_s()
        self.plan_text = plan.describe()

    def _publish(self, s: Sample) -> None:
        """A cycle's Sample: the newest for latest(), queued for the
        recorder, and folded into the per-channel display state."""
        last = dict(self._last_by_key)
        if s.readings:
            by_key = {}
            for r in s.readings:
                last[r.key] = r
                by_key[r.key] = r
            for d, src in (s.derived or {}).items():
                r = by_key.get(src)
                if r is not None and d in s.values:
                    # Dated by the reading it was computed from; request -1
                    # because it has no request of its own.
                    last[d] = Reading(d, s.values[d], r.sent, r.received, -1)
        else:
            for k, v in s.values.items():
                last[k] = Reading(k, v, s.t, s.t, -1)
        self._last_by_key = last
        if self._prev_t is not None and s.t > self._prev_t:
            gap = s.t - self._prev_t
            self._cycle_s = (gap if self._cycle_s is None else
                             self._cycle_s + self.CYCLE_EMA_ALPHA
                             * (gap - self._cycle_s))
        self._prev_t = s.t
        self._capture_plan()
        self._latest = s
        self._queue.append(s)

    def status_message(self) -> str | None:
        """One line for the Dashboard status bar while polls are failing."""
        if not self.failing():
            return None
        msg = (f"GT not answering — {self.fail_streak} polls failed: "
               f"{self.last_error}")
        if self._reconnect_attempts:
            msg += f" · reconnect attempt {self._reconnect_attempts}"
            if self.reconnect_error:
                msg += f" failed: {self.reconnect_error}"
        return msg

    def drain(self):
        out = []
        while True:
            try:
                out.append(self._queue.popleft())
            except IndexError:
                return out

    def start(self):
        self._gt.open()
        try:
            self.device = getattr(self._gt, "device", "OBDX Pro GT")
            self.port_name = getattr(self._gt, "port_name", None)
            self._t0 = time.monotonic()
            first = self._sample(self._gt.poll_timed())
        except BaseException:
            # The port is open: a failed first poll must release it, or every
            # later Connect fails with "Access is denied" until a restart.
            self._gt.close()
            raise
        if first is not None:
            self._keys = sorted(first.values.keys())
            self._publish(first)
        self._capture_plan()
        if self.plan_text:
            # Once per connect: what this link will poll, and how fast.
            _log.info("GT %s %s", self.port_name, self.plan_text)
        self._stop.clear()
        self._kick.clear()
        self._thread = threading.Thread(target=self._run, name="gt-poll", daemon=True)
        self._thread.start()

    def _sample(self, readings) -> Optional[Sample]:
        """One sweep's readings -> a Sample on this source's clock, or None
        when nothing decoded. Each value keeps its own request's exchange
        times; the Sample time is the sweep midpoint (halfway between the
        first answered request's send and the last one's reply), the single
        documented reference for the wide recording row."""
        if not readings:
            return None
        t0 = self._t0
        rebased = tuple(Reading(r.key, r.value, r.sent - t0, r.received - t0,
                                r.request) for r in readings)
        s = Sample(t=0.0, values={r.key: r.value for r in rebased},
                   readings=rebased)
        s.t = (s.sweep_start() + s.sweep_end()) / 2
        self._derive(s)
        return s

    def run_exclusive(self, fn):
        """Run fn(gt) with gauge polling held off — lets Diagnostics reuse a
        live dashboard connection without interleaving serial traffic."""
        with self._io_lock:
            return fn(self._gt)

    def read_recording_context(self) -> dict:
        """What a recording's metadata needs from the vehicle, read with the
        poll thread held (run_exclusive): the identity (Mode 09 VIN / CALID /
        CVN; cached for this connection once the VIN read, re-tried at the
        next recording otherwise), the DTCs at start (read_dtcs, every
        recording) and the supported-PID masks. Never raises: a failure is
        recorded as not examined, with why. Called off the GUI thread."""
        def read(gt):
            ctx: dict = {}
            ident = self._identity
            if ident is None:
                try:
                    ident = gt.read_vehicle_identity()
                except Exception as exc:
                    ident = {"examined": False,
                             "why": f"{type(exc).__name__}: {exc}"}
                if (ident.get("vin") or {}).get("examined"):
                    self._identity = ident
            ctx["identity"] = ident
            try:
                ctx["dtcs_at_start"] = gt.read_dtcs()
            except Exception as exc:
                ctx["dtcs_at_start"] = {
                    "examined": False, "error": f"{type(exc).__name__}: {exc}"}
            try:
                ctx["supported"] = gt.supported_report()
            except Exception as exc:
                ctx["supported"] = {"examined": False,
                                    "why": f"{type(exc).__name__}: {exc}"}
            return ctx
        return self.run_exclusive(read)

    def request_reconnect(self) -> None:
        """Skip the current backoff wait (the user pressed Connect while the
        link is down)."""
        self._kick.set()

    def _run(self):
        while not self._stop.is_set():
            if self._fails_since_open >= self.RECONNECT_AFTER:
                self._reconnect()
                continue
            try:
                with self._io_lock:
                    s = self._sample(self._gt.poll_timed())
                if s is not None:
                    self._publish(s)
                    self.fail_streak = 0
                    self._fails_since_open = 0
                    self._reconnect_attempts = 0
                    self.reconnect_error = None
                else:
                    # poll_timed() returns [] only when nothing at all decoded —
                    # not one PID and not the GT's own ATRV. A healthy GT always
                    # answers ATRV, so an empty poll is a failure, not a live
                    # source with nothing to say (it used to reset the streak
                    # and leave the gauges frozen as live).
                    self.fail_streak += 1
                    self._fails_since_open += 1
                    self.last_error = ("the GT returned no data (no PID and no "
                                       "ATRV voltage decoded)")
            except Exception as exc:
                self.fail_streak += 1
                self._fails_since_open += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self._interval if self.fail_streak == 0
                            else max(self._interval, self.FAIL_PAUSE_S))

    def _reconnect(self) -> None:
        """Close and re-open the GT once, after the backoff for this attempt.
        Never raises: the outcome is reported through status_message()."""
        if self._reconnect_attempts:
            delay = min(self.RECONNECT_MAX_S, self.RECONNECT_BASE_S
                        * 2 ** (self._reconnect_attempts - 1))
            self._kick.wait(delay)
            self._kick.clear()
        if self._stop.is_set():
            return
        self._reconnect_attempts += 1
        with self._io_lock:
            try:
                self._gt.close()
            except Exception:
                pass   # the handle of an unplugged device may refuse to close
            if not self._explicit_port:
                # Re-run VID/PID autodetect: a replugged GT can come back on a
                # different COM number.
                self._gt.port_name = None
            try:
                self._gt.open()
            except Exception as exc:
                self.reconnect_error = f"{type(exc).__name__}: {exc}"
                return
            if self._stop.is_set():
                try:
                    self._gt.close()   # stop() raced the open: don't hold the port
                except Exception:
                    pass
                return
        self.reconnect_error = None
        self.reconnects += 1
        self._identity = None        # a re-open may be a different vehicle
        self._fails_since_open = 0
        self.port_name = getattr(self._gt, "port_name", self.port_name)

    def stop(self):
        self._stop.set()
        self._kick.set()
        if self._thread:
            # A poll can be mid-read (read deadline ~1.2 s) or mid-reopen
            # (ATZ etc., ~2 s); give it time to see the stop flag.
            self._thread.join(timeout=3.0)
            self._thread = None
        # Close under the IO lock so the port is never closed underneath a
        # poll or a reopen. Bounded: a wedged thread must not hang the GUI.
        locked = self._io_lock.acquire(timeout=5.0)
        try:
            self._gt.close()
        finally:
            if locked:
                self._io_lock.release()
