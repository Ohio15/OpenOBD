"""
appsettings — the ONE way OpenOBD constructs, reads and writes its persistent
QSettings.

Every read/write of per-user settings goes through app_settings(). The
4-argument QSettings constructor is used deliberately: it honors
QSettings.setDefaultFormat() and QSettings.setPath(), so a test can redirect
ALL settings to a throwaway INI tree. The familiar 2-argument
QSettings("OpenOBD", "OpenOBD") constructor ignores both and always hits the
real per-user store (the registry on Windows) — which is how a test run once
wrote a demo Live Data layout into the user's real settings. Constructing
QSettings any other way in app code is a defect.

For the app itself nothing changes: with no redirect installed,
defaultFormat() is NativeFormat and the store is exactly the registry key the
2-argument form used.

Writes and reads that matter go through write_settings() / read_settings()
(UI preferences through save_pref()), never a bare setValue() left for Qt to
flush. Why (measured on Windows 11, PySide6 / Qt 6.11.2, 2026-10-26): a sync
of a just-written INI transiently fails with QSettings.AccessError about 1 in
75 times, because another process (Defender, the Search indexer) has the
file open for a moment. Qt then LOSES the value without saying so, and its
own retry cannot be relied on:

* QConfFileSettingsPrivate::syncConfFile returns on the failure with the
  change still pending only in the shared in-memory QConfFile (addedKeys /
  removedKeys); nothing is written.
* QSettings::sync() clears pendingChanges whatever the outcome, so the
  destructor's flush (taken only when pendingChanges is set) does not retry.
* When the last QSettings on that file is destroyed,
  ~QConfFileSettingsPrivate DELETES a QConfFile whose recorded size is 0 (the
  file did not exist, or was empty, when last read) — pending change and all.
  The next QSettings reads the file and reports the value absent with status
  NoError. A QConfFile with size > 0 is parked in Qt's unused-file cache
  instead and the next QSettings's constructor sync re-attempts the write,
  which is why the loss showed only for a fresh / emptied store.
* status() is sticky per object (QSettingsPrivate::setStatus only replaces
  NoError), so re-syncing the same object cannot report a recovery.

Hence each attempt here uses a FRESH QSettings on the same store and
RE-APPLIES the change itself (setValue / remove are idempotent), checks
status() after sync(), retries a transient AccessError a bounded number of
times, and raises on a final failure so no caller can claim a value was saved
when it was not.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional, TypeVar

from PySide6.QtCore import QSettings

ORGANIZATION = "OpenOBD"
APPLICATION = "OpenOBD"

#: Attempts for one settings write or read, and the sleep before each retry.
#: A transient AV / indexer hold measured on paxson cleared within one retry;
#: the worst case here (all attempts failing) blocks the caller ~0.4 s.
SETTINGS_ATTEMPTS = 5
SETTINGS_BACKOFF_S = (0.02, 0.05, 0.1, 0.2)

_log = logging.getLogger(__name__)
T = TypeVar("T")


class SettingsError(OSError):
    """The settings store could not be written or read."""


class SettingsWriteError(SettingsError):
    """A settings change was NOT persisted (after the bounded retries)."""


class SettingsReadError(SettingsError):
    """The settings store could not be read: a value's absence means
    nothing, it is NOT "none stored"."""


def app_settings() -> QSettings:
    return QSettings(QSettings.defaultFormat(), QSettings.UserScope,
                     ORGANIZATION, APPLICATION)


def _fresh(like: Optional[QSettings]) -> QSettings:
    """A new QSettings on the same store as `like` (the app store when None).
    New per attempt: status() is sticky per object."""
    if like is None:
        return app_settings()
    if like.organizationName():
        return QSettings(like.format(), like.scope(),
                         like.organizationName(), like.applicationName())
    return QSettings(like.fileName(), like.format())


def _status_name(status) -> str:
    return getattr(status, "name", str(status))


def _backoff(attempt: int) -> None:
    if attempt:
        time.sleep(SETTINGS_BACKOFF_S[min(attempt, len(SETTINGS_BACKOFF_S)) - 1])


def write_settings(apply: Callable[[QSettings], None],
                   like: Optional[QSettings] = None, *,
                   what: str = "settings") -> None:
    """Apply a change to the store and make it durable, or raise
    SettingsWriteError. `apply` must be idempotent (it is re-run on a fresh
    object for every attempt, see the module docstring). A FormatError (the
    store exists but cannot be parsed) is not transient: no retry."""
    status = QSettings.NoError
    for attempt in range(SETTINGS_ATTEMPTS):
        _backoff(attempt)
        st = _fresh(like)
        apply(st)
        st.sync()
        status = st.status()
        path = st.fileName()
        del st
        if status == QSettings.NoError:
            return
        if status != QSettings.AccessError:
            break
    raise SettingsWriteError(
        f"{what} not saved: {_status_name(status)} writing {path}"
        + (f" after {SETTINGS_ATTEMPTS} attempts"
           if status == QSettings.AccessError else ""))


def read_settings(read: Callable[[QSettings], T],
                  like: Optional[QSettings] = None, *,
                  what: str = "settings") -> T:
    """Run `read` against the store and return its result, or raise
    SettingsReadError when the store reports an error (a value read from a
    store that could not be loaded is not evidence of absence)."""
    status = QSettings.NoError
    for attempt in range(SETTINGS_ATTEMPTS):
        _backoff(attempt)
        st = _fresh(like)
        result = read(st)
        status = st.status()
        path = st.fileName()
        del st
        if status == QSettings.NoError:
            return result
        if status != QSettings.AccessError:
            break
    raise SettingsReadError(
        f"{what} unreadable: {_status_name(status)} reading {path}"
        + (f" after {SETTINGS_ATTEMPTS} attempts"
           if status == QSettings.AccessError else ""))


def save_pref(settings: Optional[QSettings], key: str, value) -> bool:
    """Persist a UI preference (splitter state, collapsed panels, ...);
    value None removes the key. A preference is cosmetic, so a final failure
    is logged as a warning and reported by the return value, never raised
    into a Qt slot."""
    def apply(st: QSettings) -> None:
        if value is None:
            st.remove(key)
        else:
            st.setValue(key, value)
    try:
        write_settings(apply, settings, what=f"preference {key}")
        return True
    except SettingsWriteError as exc:
        _log.warning("%s", exc)
        return False
