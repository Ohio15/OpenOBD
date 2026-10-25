"""
recorder — the Dashboard's drive datalog writer.

A drive recording must survive the things that actually happen in a truck: the
app crashing, the laptop losing power, the window being closed mid-drive, the
GT dropping off USB, a save dialog being cancelled. So this writer:

  * creates the FINAL file up front in a durable, discoverable folder (never a
    %TEMP% path the user has to be told about), so there is nothing to "save"
    at the end and nothing to lose if the end never comes;
  * writes the header immediately and flushes every batch to the OS, with an
    fsync at most every FSYNC_INTERVAL_S, so an app crash loses nothing and a
    power cut loses at most the last couple of seconds;
  * fixes its columns at start from the set of channels the source can EVER
    produce (not the subset that answered the first poll), so a PID that
    starts answering mid-drive is recorded, and a PID that stops answering
    leaves blank cells rather than shifting columns;
  * never raises into the caller's paint loop: a write failure (disk full,
    folder removed) is captured in `error`, the file is closed, and the
    samples written up to that point stay on disk;
  * keeps ONE wide row per sample (one GT poll sweep), so every Log Analysis
    joint feature (regimes, overlay, shift detection) reads it unchanged. A
    channel that did not answer in that sweep is a blank cell, which
    logbin._to_float reads as missing. For a source that supplies
    Sample.readings (the GT) the row's "Time (s)" is the SWEEP MIDPOINT, a
    documented reference: every value in the row was sampled within half a
    sweep of it. The old stamp was the sweep END, which aged RPM by ~0.95 s
    and load by ~1.33 s and put pedal after throttle (brain 8d92fae5);
  * writes a lossless long-format sidecar next to the CSV, <stem>.long.csv,
    one line per value: its own request's midpoint, channel, full-precision
    value, request number, and the sweep's start and end (so a reader can
    bound the wide row's skew). Same flush / fsync / finalize discipline as
    the CSV. This sidecar is the precise record of when each value was
    sampled; Log Analysis does not read it yet;
  * zeroes time at the first recorded sample (both files share the zero, so
    the first sweep's earliest requests sit slightly below 0 in the
    sidecar), not at GT connect.

No Qt here, so the headless suite exercises it directly.
"""
from __future__ import annotations

import csv
import os
import time
from datetime import datetime
from typing import Callable, Iterable, Optional, Sequence

#: Upper bound on how long written rows can sit in the OS cache before they
#: are forced to disk. A flush happens on every write() regardless.
FSYNC_INTERVAL_S = 2.0

TIME_LABEL = "Time (s)"

#: The long-format sidecar: <stem>.long.csv beside <stem>.csv.
SIDECAR_SUFFIX = ".long.csv"
SIDECAR_HEADER = ["Time (s)", "Channel", "Value", "Request",
                  "Sweep Start (s)", "Sweep End (s)"]


def default_log_dirs(documents: Optional[str], app_data: Optional[str]) -> list[str]:
    """Candidate folders for recordings, most discoverable first: the user's
    real Documents folder (the caller resolves it through the OS known-folder
    API, which follows a OneDrive redirect that ~/Documents does not), then a
    per-user app-data folder as the fallback when Documents is unavailable."""
    out = []
    if documents:
        out.append(os.path.join(documents, "OpenOBD", "Logs"))
    if app_data:
        out.append(os.path.join(app_data, "Logs"))
    return out


def _fmt(v) -> str:
    return "" if v is None else f"{v:g}"


def sidecar_path(csv_path: str) -> str:
    """The long-format sidecar that belongs to a recording's CSV."""
    stem, _ext = os.path.splitext(csv_path)
    return stem + SIDECAR_SUFFIX


def _sidecar_lines(sample, zero: float) -> list[list[str]]:
    """The long-format lines for one sample: every value the source produced
    (including keys with no CSV column), at full float precision. A sample
    without readings (log replay, truck-mcp) has no request identity and no
    sweep, so its values carry the sample time and blank request columns."""
    readings = getattr(sample, "readings", None)
    if not readings:
        rel = f"{sample.t - zero:.6f}"
        return [[rel, k, repr(float(v)), "", "", ""]
                for k, v in sample.values.items()]
    start = f"{sample.sweep_start() - zero:.6f}"
    end = f"{sample.sweep_end() - zero:.6f}"
    return [[f"{r.t - zero:.6f}", r.key, repr(float(r.value)), str(r.request),
             start, end] for r in readings]


class DriveRecorder:
    """Streams Samples (t, {key: value}) to a CSV that Log Analysis reloads.

    keys:   canonical channel keys, in column order.
    labels: key -> column header (must map back to the key via
            logbin.map_channel; app.RECORD_LABELS is checked for that).
    dirs:   candidate folders, tried in order until one is writable.
    """

    def __init__(self, keys: Sequence[str], labels: dict, dirs: Iterable[str],
                 prefix: str = "gt_log",
                 now: Callable[[], datetime] = datetime.now,
                 fsync_interval: float = FSYNC_INTERVAL_S):
        self.keys = list(keys)
        self.count = 0
        self.error: Optional[str] = None
        self.ignored_keys: set[str] = set()   # keys seen with no column
        self._fsync_interval = fsync_interval
        self._last_fsync = time.monotonic()
        self._fh = None
        self._writer = None
        self._long_fh = None
        self._long_writer = None
        # Source time of the first recorded sample; both files are relative
        # to it, so the recording starts at 0 rather than at GT connect.
        self._t_zero: Optional[float] = None
        stamp = now().strftime("%Y%m%d_%H%M%S")
        failures = []
        for d in dirs:
            try:
                os.makedirs(d, exist_ok=True)
                (self._fh, self.path,
                 self._long_fh, self.long_path) = self._create(d, f"{prefix}_{stamp}")
                break
            except OSError as exc:
                failures.append(f"{d}: {exc}")
        if self._fh is None:
            raise OSError("no writable folder for the recording — "
                          + ("; ".join(failures) or "no folder configured"))
        self._writer = csv.writer(self._fh)
        self._long_writer = csv.writer(self._long_fh)
        try:
            self._writer.writerow([TIME_LABEL] + [labels[k] for k in self.keys])
            self._long_writer.writerow(SIDECAR_HEADER)
            self._sync(force=True)
        except OSError:
            self._close_quietly()
            for p in (self.path, self.long_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            raise

    @staticmethod
    def _create(folder: str, stem: str):
        """Open a NEW csv and its sidecar exclusively ('x'): two recordings
        started in the same second, or leftover files, must never be
        overwritten, and a csv is never paired with another run's sidecar."""
        for n in range(1000):
            name = f"{stem}.csv" if n == 0 else f"{stem}_{n}.csv"
            path = os.path.join(folder, name)
            try:
                fh = open(path, "x", newline="", encoding="utf-8")
            except FileExistsError:
                continue
            long_path = sidecar_path(path)
            try:
                long_fh = open(long_path, "x", newline="", encoding="utf-8")
            except FileExistsError:
                # A stray sidecar owns this stem: release the csv, try the next.
                fh.close()
                os.unlink(path)
                continue
            except OSError:
                fh.close()
                os.unlink(path)
                raise
            return fh, path, long_fh, long_path
        raise OSError(f"could not create a unique file for {stem} in {folder}")

    @property
    def active(self) -> bool:
        return self._fh is not None

    def _sync(self, force: bool = False) -> None:
        self._fh.flush()
        self._long_fh.flush()
        now = time.monotonic()
        if force or now - self._last_fsync >= self._fsync_interval:
            os.fsync(self._fh.fileno())
            os.fsync(self._long_fh.fileno())
            self._last_fsync = now

    def write(self, samples) -> int:
        """Append samples; returns how many rows were written. On an IO error
        the recorder stops (error set, file closed) and returns what it had
        written in this call — earlier rows are already on disk."""
        if self._fh is None:
            return 0
        n = 0
        try:
            for s in samples:
                vals = s.values
                extra = vals.keys() - set(self.keys)
                if extra:
                    self.ignored_keys |= extra
                if self._t_zero is None:
                    self._t_zero = s.t
                self._writer.writerow(
                    [f"{s.t - self._t_zero:.3f}"]
                    + [_fmt(vals.get(k)) for k in self.keys])
                self._long_writer.writerows(_sidecar_lines(s, self._t_zero))
                n += 1
            self.count += n
            if n:
                self._sync()
        except (OSError, ValueError) as exc:
            # ValueError: write to a file closed underneath us.
            self.count += n
            self.error = f"{type(exc).__name__}: {exc}"
            self._close_quietly()
        return n

    def _close_quietly(self) -> None:
        handles = (self._fh, self._long_fh)
        self._fh = self._writer = self._long_fh = self._long_writer = None
        for fh in handles:
            if fh is None:
                continue
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except (OSError, ValueError):
                pass
            try:
                fh.close()
            except OSError:
                pass

    def close(self) -> tuple[str, int]:
        """Flush, fsync and close both files. An empty recording (header only)
        is removed with its sidecar so aborted starts don't litter the folder.
        Returns (path, rows)."""
        if self._fh is not None:
            try:
                self._sync(force=True)
            except (OSError, ValueError) as exc:
                self.error = self.error or f"{type(exc).__name__}: {exc}"
            self._close_quietly()
        if self.count == 0 and self.error is None:
            for p in (self.path, self.long_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        return self.path, self.count
