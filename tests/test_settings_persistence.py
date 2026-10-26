"""
Transient write failures on Windows (brain 8d13ce7f, found in 129af1c0):
replacing a just-written file fails now and then with ERROR_ACCESS_DENIED /
ERROR_SHARING_VIOLATION because Defender or the Search indexer has it open.

These tests inject the failure DETERMINISTICALLY through the real
dependencies (a read-only INI, an exclusively opened INI, an open handle on
the replace target), never through a double that encodes the assumption:

* QSettings (Qt 6.11): a failed sync() reports AccessError, the change is
  then LOST in-process for an empty store, and status() is sticky per object
  -- why openobd.appsettings retries on a FRESH object and re-applies.
* os.replace over a file another handle holds raises PermissionError with
  winerror 5 -- why recorder._replace_retrying retries exactly that.
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QSettings  # noqa: E402

from openobd import appsettings as aps  # noqa: E402
from openobd import recorder as recmod  # noqa: E402
from openobd.pedal import PedalCal  # noqa: E402

VIN = "3GCRKTE35AG150432"
RW = stat.S_IREAD | stat.S_IWRITE
WIN = sys.platform == "win32"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(qapp, tmp_path):
    """The app store (app_settings()) redirected to a NEW INI tree, so no
    QConfFile for it is cached in Qt from an earlier test. Returns the
    primary INI path (written first, read first)."""
    root = tmp_path / "store"
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(root))
    primary = root / aps.ORGANIZATION / f"{aps.APPLICATION}.ini"
    assert os.path.normcase(os.path.normpath(aps.app_settings().fileName())) \
        == os.path.normcase(str(primary))
    primary.parent.mkdir(parents=True, exist_ok=True)
    yield primary
    if primary.exists():
        os.chmod(primary, RW)


@pytest.fixture
def attempts(monkeypatch):
    """Record each retry (the attempt number passed to _backoff) without
    sleeping; a test may hang an action on a given attempt."""
    seen: list[int] = []
    hooks: dict = {}

    def backoff(attempt):
        if attempt:
            seen.append(attempt)
            if attempt in hooks:
                hooks.pop(attempt)()
    monkeypatch.setattr(aps, "_backoff", backoff)
    return seen, hooks


def _read_only_empty(path):
    path.write_bytes(b"")
    os.chmod(path, stat.S_IREAD)


# --------------------------------------------------------------------------- #
# The real Qt semantics the fix depends on
# --------------------------------------------------------------------------- #
def test_qt_reports_a_failed_sync_and_then_loses_the_value(qapp, tmp_path):
    path = tmp_path / "lost.ini"
    _read_only_empty(path)                     # exists, empty: size 0
    st = QSettings(str(path), QSettings.IniFormat)
    st.setValue("k", "v")
    st.sync()
    assert st.status() == QSettings.AccessError
    del st                                     # last ref: QConfFile deleted
    os.chmod(path, RW)
    fresh = QSettings(str(path), QSettings.IniFormat)
    assert fresh.value("k") is None            # gone, and no error says so
    assert fresh.status() == QSettings.NoError


def test_qt_status_is_sticky_so_a_resync_cannot_report_recovery(qapp,
                                                                tmp_path):
    path = tmp_path / "sticky.ini"
    _read_only_empty(path)
    st = QSettings(str(path), QSettings.IniFormat)
    st.setValue("k", "v")
    st.sync()
    assert st.status() == QSettings.AccessError
    os.chmod(path, RW)
    st.sync()                                  # this write succeeds ...
    assert "k=v" in path.read_text(encoding="utf-8")
    assert st.status() == QSettings.AccessError   # ... but status says not


# --------------------------------------------------------------------------- #
# write_settings / save_pedal_cal / save_pref
# --------------------------------------------------------------------------- #
def test_a_transient_write_failure_is_retried_on_a_fresh_object(store,
                                                                attempts):
    from openobd.app import load_pedal_cal, save_pedal_cal
    seen, hooks = attempts
    _read_only_empty(store)                    # the loss case: empty store
    hooks[1] = lambda: os.chmod(store, RW)     # the scanner lets go
    save_pedal_cal(VIN, PedalCal(18.0, 65.0, "learned"))
    assert seen == [1]
    assert "rest=18" in store.read_text(encoding="utf-8")   # on disk
    cal = load_pedal_cal(VIN)
    assert (cal.rest, cal.full, cal.source) == (18.0, 65.0, "learned")


def test_each_attempt_reapplies_the_change_to_a_new_object(store, attempts):
    _seen, hooks = attempts
    _read_only_empty(store)
    hooks[2] = lambda: os.chmod(store, RW)
    objs = []

    def apply(st):
        objs.append(st)
        st.setValue("pref/k", "v")
    aps.write_settings(apply)
    assert len(objs) == 3
    assert len({id(o) for o in objs}) == 3
    assert "k=v" in store.read_text(encoding="utf-8")


def test_a_persistent_write_failure_raises_after_the_bounded_attempts(
        store, attempts):
    from openobd.app import save_pedal_cal
    seen, _hooks = attempts
    _read_only_empty(store)
    with pytest.raises(aps.SettingsWriteError) as exc:
        save_pedal_cal(VIN, PedalCal(18.0, 65.0, "learned"))
    assert seen == list(range(1, aps.SETTINGS_ATTEMPTS))
    assert "AccessError" in str(exc.value) and VIN in str(exc.value)
    assert f"{aps.SETTINGS_ATTEMPTS} attempts" in str(exc.value)


def test_a_format_error_is_not_retried(store, attempts):
    seen, _hooks = attempts
    store.write_text("[a/b/c]\nk=1\n", encoding="utf-8")   # unparseable
    with pytest.raises(aps.SettingsWriteError, match="FormatError"):
        aps.write_settings(lambda st: st.setValue("x", 1))
    assert seen == []


def test_save_pref_logs_and_returns_false_instead_of_raising(store, attempts,
                                                             caplog):
    _read_only_empty(store)
    with caplog.at_level("WARNING", logger="openobd.appsettings"):
        assert aps.save_pref(None, "tuning/overlay_panel_open",
                             "true") is False
    os.chmod(store, RW)
    assert "tuning/overlay_panel_open" in caplog.text
    assert aps.save_pref(None, "tuning/overlay_panel_open", "true") is True
    assert aps.app_settings().value("tuning/overlay_panel_open") == "true"
    assert aps.save_pref(None, "tuning/overlay_panel_open", None) is True
    assert aps.app_settings().value("tuning/overlay_panel_open") is None


def test_save_pref_writes_through_an_injected_file_store(qapp, tmp_path):
    ini = tmp_path / "page.ini"
    page_settings = QSettings(str(ini), QSettings.IniFormat)
    assert aps.save_pref(page_settings, "diag/notes_open", "false") is True
    assert page_settings.value("diag/notes_open") == "false"  # same object
    assert "notes_open=false" in ini.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# read_settings / load_pedal_cal / try_load_pedal_cal
# --------------------------------------------------------------------------- #
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3


def _hold_exclusive(path):
    """Open `path` with NO sharing (as a scanner might): every other open
    fails with ERROR_SHARING_VIOLATION until closed. Returns a closer."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.CreateFileW(str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0,
                        None)
    invalid = ctypes.c_void_p(-1).value
    assert h is not None and h != invalid, ctypes.get_last_error()
    closed = []

    def close():
        if not closed:
            closed.append(k32.CloseHandle(h))
    return close


def _write_stored_ini(path):
    """Write the store with plain file I/O: Qt has never read it, so a
    QSettings must really open it (no cached QConfFile short-circuit)."""
    path.write_text(f"[pedal_cal]\n{VIN}\\rest=18\n{VIN}\\full=65\n"
                    f"{VIN}\\source=stored\n", encoding="utf-8")


@pytest.mark.skipif(not WIN, reason="Windows share-mode semantics")
def test_a_transient_read_failure_is_retried(store, attempts):
    from openobd.app import load_pedal_cal
    seen, hooks = attempts
    _write_stored_ini(store)
    close = _hold_exclusive(store)
    hooks[1] = close
    try:
        cal = load_pedal_cal(VIN)
    finally:
        close()
    assert seen == [1]
    assert (cal.rest, cal.full) == (18.0, 65.0)


@pytest.mark.skipif(not WIN, reason="Windows share-mode semantics")
def test_an_unreadable_store_is_an_error_not_none_stored(store, attempts):
    from openobd.app import load_pedal_cal, try_load_pedal_cal
    seen, _hooks = attempts
    _write_stored_ini(store)
    close = _hold_exclusive(store)
    try:
        cal, err = try_load_pedal_cal(VIN)
        with pytest.raises(aps.SettingsReadError):
            load_pedal_cal(VIN)
    finally:
        close()
    assert cal is None
    assert err.startswith("SettingsReadError") and "AccessError" in err
    assert seen == list(range(1, aps.SETTINGS_ATTEMPTS)) * 2


def test_an_unparseable_store_is_an_error_not_none_stored(store, attempts):
    from openobd.app import try_load_pedal_cal
    store.write_text("[a/b/c]\nk=1\n", encoding="utf-8")
    cal, err = try_load_pedal_cal(VIN)
    assert cal is None and "FormatError" in err
    assert attempts[0] == []


def test_none_stored_is_still_none_without_error(store):
    from openobd.app import try_load_pedal_cal
    assert try_load_pedal_cal(VIN) == (None, None)


# --------------------------------------------------------------------------- #
# recorder: the metadata atomic rewrite
# --------------------------------------------------------------------------- #
def _recorder(tmp_path):
    return recmod.DriveRecorder(["rpm"], {"rpm": "RPM"}, [str(tmp_path)])


@pytest.fixture
def sleeps(monkeypatch):
    seen: list[float] = []
    hooks: dict = {}

    def sleep(s):
        seen.append(s)
        if len(seen) in hooks:
            hooks.pop(len(seen))()
    monkeypatch.setattr(recmod.time, "sleep", sleep)
    return seen, hooks


def _permission_error(winerror):
    exc = PermissionError(13, "Access is denied")
    exc.winerror = winerror
    return exc


@pytest.mark.parametrize("winerror", [5, 32])
def test_a_transient_replace_failure_is_retried(tmp_path, monkeypatch,
                                                sleeps, winerror):
    rec = _recorder(tmp_path)
    real = os.replace
    calls = []

    def replace(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise _permission_error(winerror)
        return real(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    assert rec.update_meta(note="after the hold") is True
    assert len(calls) == 3
    assert sleeps[0] == list(recmod.REPLACE_BACKOFF_S[:2])
    assert rec.meta_error is None
    assert recmod.read_meta(rec.path)["note"] == "after the hold"
    assert not os.path.exists(rec.meta_path + ".tmp")
    monkeypatch.setattr(os, "replace", real)
    rec.close()


@pytest.mark.parametrize("make_exc", [
    lambda: OSError("power cut before the rename"),
    lambda: _permission_error(None),
    lambda: _permission_error(1314),           # not a sharing failure
])
def test_a_non_transient_replace_failure_fails_on_the_first_try(
        tmp_path, monkeypatch, sleeps, make_exc):
    rec = _recorder(tmp_path)
    good = recmod.read_meta(rec.path)
    calls = []

    def replace(src, dst):
        calls.append(dst)
        raise make_exc()
    monkeypatch.setattr(os, "replace", replace)
    assert rec.update_meta(note="after the hold") is False
    assert len(calls) == 1 and sleeps[0] == []
    assert rec.meta_error and recmod.read_meta(rec.path) == good
    assert not os.path.exists(rec.meta_path + ".tmp")


def test_a_persistent_transient_failure_ends_in_meta_error(tmp_path,
                                                           monkeypatch,
                                                           sleeps):
    rec = _recorder(tmp_path)
    good = recmod.read_meta(rec.path)
    calls = []

    def replace(src, dst):
        calls.append(dst)
        raise _permission_error(5)
    monkeypatch.setattr(os, "replace", replace)
    assert rec.update_meta(note="after the hold") is False
    assert len(calls) == len(recmod.REPLACE_BACKOFF_S) + 1
    assert sleeps[0] == list(recmod.REPLACE_BACKOFF_S)
    assert "PermissionError" in rec.meta_error
    assert recmod.read_meta(rec.path) == good
    assert not os.path.exists(rec.meta_path + ".tmp")


@pytest.mark.skipif(not WIN, reason="Windows delete-share semantics")
def test_a_real_held_target_fails_with_winerror_5_and_recovers(tmp_path,
                                                               sleeps):
    rec = _recorder(tmp_path)
    holder = open(rec.meta_path, "rb")   # no FILE_SHARE_DELETE: blocks replace
    try:
        probe = rec.meta_path + ".probe"
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("{}")
        with pytest.raises(PermissionError) as real_exc:
            os.replace(probe, rec.meta_path)
        os.unlink(probe)
        assert real_exc.value.winerror in recmod.TRANSIENT_WINERRORS
        sleeps[1][1] = holder.close      # released during the first backoff
        assert rec.update_meta(note="after the hold") is True
    finally:
        holder.close()
    assert sleeps[0] == [recmod.REPLACE_BACKOFF_S[0]]
    assert rec.meta_error is None
    assert recmod.read_meta(rec.path)["note"] == "after the hold"
    rec.close()
