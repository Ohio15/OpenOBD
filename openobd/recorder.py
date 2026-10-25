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
    samples written up to that point stay on disk.

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
        stamp = now().strftime("%Y%m%d_%H%M%S")
        failures = []
        for d in dirs:
            try:
                os.makedirs(d, exist_ok=True)
                self._fh, self.path = self._create(d, f"{prefix}_{stamp}")
                break
            except OSError as exc:
                failures.append(f"{d}: {exc}")
        if self._fh is None:
            raise OSError("no writable folder for the recording — "
                          + ("; ".join(failures) or "no folder configured"))
        self._writer = csv.writer(self._fh)
        self._writer.writerow([TIME_LABEL] + [labels[k] for k in self.keys])
        self._sync(force=True)

    @staticmethod
    def _create(folder: str, stem: str):
        """Open a NEW file exclusively ('x'): two recordings started in the
        same second, or a leftover file, must never be overwritten."""
        for n in range(1000):
            name = f"{stem}.csv" if n == 0 else f"{stem}_{n}.csv"
            path = os.path.join(folder, name)
            try:
                return open(path, "x", newline="", encoding="utf-8"), path
            except FileExistsError:
                continue
        raise OSError(f"could not create a unique file for {stem} in {folder}")

    @property
    def active(self) -> bool:
        return self._fh is not None

    def _sync(self, force: bool = False) -> None:
        self._fh.flush()
        now = time.monotonic()
        if force or now - self._last_fsync >= self._fsync_interval:
            os.fsync(self._fh.fileno())
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
                self._writer.writerow(
                    [f"{s.t:.2f}"] + [_fmt(vals.get(k)) for k in self.keys])
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
        fh, self._fh, self._writer = self._fh, None, None
        if fh is None:
            return
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
        """Flush, fsync and close. An empty recording (header only) is removed
        so aborted starts don't litter the folder. Returns (path, rows)."""
        if self._fh is not None:
            try:
                self._sync(force=True)
            except (OSError, ValueError) as exc:
                self.error = self.error or f"{type(exc).__name__}: {exc}"
            self._close_quietly()
        if self.count == 0 and self.error is None:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        return self.path, self.count
