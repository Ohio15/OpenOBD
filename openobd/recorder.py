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
    sidecar), not at GT connect;
  * writes DERIVED values (Sample.derived, e.g. the normalized "pedal"
    computed from APP D) in the wide CSV as their own column, and in the long
    sidecar in their own line form: the time of the reading they were
    computed from, and "derived:<source key>" in the Request column (never a
    request number, so a reader can never take one for a module request);
  * writes a metadata sidecar, <stem>.meta.json (META_SCHEMA): start times
    (UTC and local), the file names, and whatever the caller knows about the
    source and the vehicle (identity, supported PIDs, DTCs at start, pedal
    calibration, channel notes), plus the user's markers. It is created with
    the other two (exclusive, flushed, fsynced) and REWRITTEN ATOMICALLY
    (temp file, fsync, os.replace) when markers or vehicle facts are added
    and at close (end time, row count, error), so a crash mid-rewrite leaves
    the previous complete file, never a torn one. A metadata write failure
    is reported in meta_error and never stops the CSV.

No Qt here, so the headless suite exercises it directly.
"""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional, Sequence

from . import __version__

#: Upper bound on how long written rows can sit in the OS cache before they
#: are forced to disk. A flush happens on every write() regardless.
FSYNC_INTERVAL_S = 2.0

TIME_LABEL = "Time (s)"

#: The long-format sidecar: <stem>.long.csv beside <stem>.csv.
SIDECAR_SUFFIX = ".long.csv"
SIDECAR_HEADER = ["Time (s)", "Channel", "Value", "Request",
                  "Sweep Start (s)", "Sweep End (s)"]

#: The metadata sidecar: <stem>.meta.json beside <stem>.csv.
META_SUFFIX = ".meta.json"
META_SCHEMA = 1
#: Prefix of the long sidecar's Request column on a derived value's line.
DERIVED_REQUEST_PREFIX = "derived:"


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


def meta_path(csv_path: str) -> str:
    """The metadata sidecar that belongs to a recording's CSV."""
    stem, _ext = os.path.splitext(csv_path)
    return stem + META_SUFFIX


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def meta_shape_problem(meta) -> Optional[str]:
    """Why a parsed .meta.json cannot be used, or None. The file is on disk
    and anyone can edit it, so every part Log Analysis consumes is checked
    for shape before use: a hostile or damaged file is reported, never
    crashes the loader, and the log loads without it."""
    if not isinstance(meta, dict):
        return "not a JSON object"
    if "schema" not in meta:
        return "no schema field (not an OpenOBD recording metadata file)"
    vehicle = meta.get("vehicle")
    if vehicle is not None:
        if not isinstance(vehicle, dict):
            return "vehicle is not an object"
        vin = vehicle.get("vin")
        if vin is not None:
            if not isinstance(vin, dict):
                return "vehicle.vin is not an object"
            if not isinstance(vin.get("examined"), bool):
                return "vehicle.vin.examined is not true/false"
            if "value" in vin and not isinstance(vin["value"], str):
                return "vehicle.vin.value is not a string"
    pc = meta.get("pedal_calibration")
    if pc is not None:
        if not isinstance(pc, dict):
            return "pedal_calibration is not an object"
        for key in ("at_start", "after_identity"):
            if pc.get(key) is not None and not isinstance(pc[key], dict):
                return f"pedal_calibration.{key} is not an object"
        changes = pc.get("changes")
        if changes is not None and not (
                isinstance(changes, list)
                and all(c is None or isinstance(c, dict) for c in changes)):
            return "pedal_calibration.changes is not a list of objects"
    markers = meta.get("markers")
    if markers is not None:
        if not isinstance(markers, list):
            return "markers is not a list"
        for m in markers:
            if not (isinstance(m, dict) and _is_num(m.get("t"))
                    and isinstance(m.get("label"), str)):
                return "a marker is not {t: number, label: text}"
    return None


def read_meta(csv_path: str) -> Optional[dict]:
    """The metadata beside a CSV, or None when there is none. Raises
    ValueError (with the reason) for a file that is not usable OpenOBD
    recording metadata: bad JSON or a shape meta_shape_problem rejects."""
    p = meta_path(csv_path)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as fh:
        try:
            meta = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not valid JSON ({exc})") from exc
    problem = meta_shape_problem(meta)
    if problem:
        raise ValueError(problem)
    return meta


def _sidecar_lines(sample, zero: float) -> list[list[str]]:
    """The long-format lines for one sample: every value the source produced
    (including keys with no CSV column), at full float precision. A sample
    without readings (log replay, truck-mcp) has no request identity and no
    sweep, so its values carry the sample time and blank request columns.
    A derived value gets its source reading's time and "derived:<source>"."""
    readings = getattr(sample, "readings", None)
    derived = getattr(sample, "derived", None) or {}
    if not readings:
        rel = f"{sample.t - zero:.6f}"
        return [[rel, k, repr(float(v)),
                 DERIVED_REQUEST_PREFIX + derived[k] if k in derived else "",
                 "", ""]
                for k, v in sample.values.items()]
    start = f"{sample.sweep_start() - zero:.6f}"
    end = f"{sample.sweep_end() - zero:.6f}"
    lines = [[f"{r.t - zero:.6f}", r.key, repr(float(r.value)),
              str(r.request), start, end] for r in readings]
    by_key = {r.key: r for r in readings}
    for key, src in derived.items():
        r = by_key.get(src)
        if r is None or key not in sample.values:
            continue
        lines.append([f"{r.t - zero:.6f}", key, repr(float(sample.values[key])),
                      DERIVED_REQUEST_PREFIX + src, start, end])
    return lines


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
                 fsync_interval: float = FSYNC_INTERVAL_S,
                 meta: Optional[dict] = None,
                 utc_now: Callable[[], datetime] = _utc_now):
        """meta: caller facts for the metadata sidecar (source, vehicle,
        supported PIDs, DTCs at start, pedal calibration, channel notes);
        merged under the recorder's own fields."""
        self.keys = list(keys)
        self.count = 0
        self.error: Optional[str] = None
        self.meta_error: Optional[str] = None
        self._utc_now = utc_now
        self._now = now
        self.ignored_keys: set[str] = set()   # keys seen with no column
        self._fsync_interval = fsync_interval
        self._last_fsync = time.monotonic()
        self._fh = None
        self._writer = None
        self._long_fh = None
        self._long_writer = None
        self.meta_path: Optional[str] = None
        self.markers: list[dict] = []
        self._last_t: Optional[float] = None
        # Source time of the first recorded sample; both files are relative
        # to it, so the recording starts at 0 rather than at GT connect.
        self._t_zero: Optional[float] = None
        local_start = now()
        utc_start = utc_now()
        stamp = local_start.strftime("%Y%m%d_%H%M%S")
        failures = []
        meta_fh = None
        for d in dirs:
            try:
                os.makedirs(d, exist_ok=True)
                (self._fh, self.path, self._long_fh, self.long_path,
                 meta_fh, self.meta_path) = self._create(d, f"{prefix}_{stamp}")
                break
            except OSError as exc:
                failures.append(f"{d}: {exc}")
        if self._fh is None:
            raise OSError("no writable folder for the recording — "
                          + ("; ".join(failures) or "no folder configured"))
        self._writer = csv.writer(self._fh)
        self._long_writer = csv.writer(self._long_fh)
        self.meta = dict(meta or {})
        self.meta.update({
            "schema": META_SCHEMA,
            "openobd_version": __version__,
            "utc_start": utc_start.isoformat(),
            "local_start": local_start.isoformat(),
            "files": {"csv": os.path.basename(self.path),
                      "long": os.path.basename(self.long_path),
                      "meta": os.path.basename(self.meta_path)},
            "columns": {k: labels[k] for k in self.keys},
            "markers": self.markers,
            "time_zero": "the first recorded sample (both CSVs and the "
                         "marker times share it)",
        })
        try:
            self._writer.writerow([TIME_LABEL] + [labels[k] for k in self.keys])
            self._long_writer.writerow(SIDECAR_HEADER)
            self._sync(force=True)
            meta_fh.write(self._meta_text())
            meta_fh.flush()
            os.fsync(meta_fh.fileno())
            meta_fh.close()
        except (OSError, TypeError, ValueError):
            self._close_quietly()
            try:
                meta_fh.close()
            except OSError:
                pass
            for p in (self.path, self.long_path, self.meta_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            raise

    @staticmethod
    def _create(folder: str, stem: str):
        """Open a NEW csv and its two sidecars exclusively ('x'): two
        recordings started in the same second, or leftover files, must never
        be overwritten, and a csv is never paired with another run's
        sidecar or metadata."""
        for n in range(1000):
            name = f"{stem}.csv" if n == 0 else f"{stem}_{n}.csv"
            path = os.path.join(folder, name)
            try:
                fh = open(path, "x", newline="", encoding="utf-8")
            except FileExistsError:
                continue
            opened = [(fh, path)]
            try:
                for side in (sidecar_path(path), meta_path(path)):
                    opened.append((open(side, "x", newline="",
                                        encoding="utf-8"), side))
            except FileExistsError:
                # A stray sidecar owns this stem: release ours, try the next.
                for h, p in opened:
                    h.close()
                    os.unlink(p)
                continue
            except OSError:
                for h, p in opened:
                    h.close()
                    os.unlink(p)
                raise
            (fh, path), (long_fh, long_path), (meta_fh, mpath) = opened
            return fh, path, long_fh, long_path, meta_fh, mpath
        raise OSError(f"could not create a unique file for {stem} in {folder}")

    # -- metadata sidecar ---------------------------------------------------- #
    def _meta_text(self) -> str:
        return json.dumps(self.meta, indent=2, sort_keys=False,
                          default=str) + "\n"

    def _write_meta(self) -> bool:
        """Atomic rewrite: the whole document to <meta>.tmp, flushed and
        fsynced, then os.replace over the old file. An interruption at any
        point leaves either the old or the new complete document."""
        if self.meta_path is None:
            return False
        tmp = self.meta_path + ".tmp"
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as fh:
                fh.write(self._meta_text())
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.meta_path)
            self.meta_error = None     # the file on disk is current again
            return True
        except (OSError, TypeError, ValueError) as exc:
            self.meta_error = f"{type(exc).__name__}: {exc}"
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    def flush_meta(self) -> bool:
        """Rewrite the metadata now (callers that batch changes)."""
        return self._write_meta()

    def update_meta(self, **fields) -> bool:
        """Merge facts learned after start (vehicle identity, DTCs, pedal
        calibration changes) and rewrite the metadata atomically."""
        self.meta.update(fields)
        return self._write_meta()

    @property
    def t_zero(self) -> Optional[float]:
        return self._t_zero

    def recording_time(self, source_t: Optional[float] = None) -> Optional[float]:
        """Seconds on the recording's zero for a source-clock time (or the
        last recorded sample when the source has no clock), None before the
        first sample."""
        if self._t_zero is None:
            return None
        t = self._last_t if source_t is None else source_t
        return None if t is None else t - self._t_zero

    def add_marker(self, source_t: Optional[float] = None,
                   label: Optional[str] = None,
                   write: bool = True) -> Optional[dict]:
        """Append a marker at the current recording time and (unless the
        caller batches rewrites, write=False, then flush_meta) rewrite the
        metadata. The CSVs are untouched. Returns the marker, or None when
        there is no time yet (no sample recorded) or the recording closed."""
        if self._fh is None:
            return None
        t = self.recording_time(source_t)
        if t is None:
            return None
        marker = {"t": round(t, 3),
                  "label": label or f"mark {len(self.markers) + 1}"}
        self.markers.append(marker)
        if write:
            self._write_meta()
        return marker

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
                self._last_t = s.t
                self._writer.writerow(
                    [f"{s.t - self._t_zero:.3f}"]
                    + [_fmt(vals.get(k)) for k in self.keys])
                self._long_writer.writerows(_sidecar_lines(s, self._t_zero))
                n += 1
            self.count += n
            if n:
                self._sync()
        except (OSError, ValueError, TypeError) as exc:
            # ValueError: write to a file closed underneath us. TypeError: a
            # malformed sample (a non-numeric value) - the recorder must
            # never raise into the paint loop; it stops with the error set.
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
        """Flush, fsync and close the files and write the final metadata
        (end time, rows, error). An empty recording (header only) is removed
        with both sidecars so aborted starts don't litter the folder.
        Idempotent. Returns (path, rows)."""
        was_open = self._fh is not None
        if was_open:
            try:
                self._sync(force=True)
            except (OSError, ValueError) as exc:
                self.error = self.error or f"{type(exc).__name__}: {exc}"
            self._close_quietly()
        if self.count == 0 and self.error is None:
            for p in (self.path, self.long_path, self.meta_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            return self.path, self.count
        if was_open or "utc_end" not in self.meta:
            self.meta.update({
                "utc_end": self._utc_now().isoformat(),
                "local_end": self._now().isoformat(),
                "rows": self.count,
                "duration_s": (None if self._last_t is None
                               else round(self._last_t - self._t_zero, 3)),
                "error": self.error,
                "ignored_keys": sorted(self.ignored_keys),
            })
            self._write_meta()
        return self.path, self.count
