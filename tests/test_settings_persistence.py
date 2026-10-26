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

import ast
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
    """Keeping every attempt's QSettings alive (objs) keeps the shared
    QConfFile, so attempt 1's constructor replays attempt 0's pending change
    into the still read-only file and fails: an attempt whose load failed
    does NOT apply (its view cannot be trusted). Attempt 2 loads once the
    hold is gone and re-applies on its own, new, object."""
    seen, hooks = attempts
    _read_only_empty(store)
    hooks[2] = lambda: os.chmod(store, RW)
    objs = []

    def apply(st):
        objs.append(st)
        st.setValue("pref/k", "v")
    aps.write_settings(apply)
    assert seen == [1, 2]
    assert len(objs) == 2
    assert len({id(o) for o in objs}) == 2
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


# --------------------------------------------------------------------------- #
# A final write failure is reconciled: the rejected value never reaches disk
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prior", ["k=old\n", ""], ids=["existed", "absent"])
def test_a_rejected_value_never_reaches_disk_through_a_later_write(
        store, attempts, prior):
    """PROBED (Qt 6.11.2, 2026-10-26): on a NON-EMPTY store the rejected
    change stays pending in the parked QConfFile and the next successful
    write on that file -- any key, any QSettings -- wrote it too: a value the
    UI had reported NOT SAVED reached disk later. write_settings now puts the
    touched keys back on a final failure."""
    store.write_text(f"[pref]\nother=1\n{prior}", encoding="utf-8")
    os.chmod(store, stat.S_IREAD)              # every attempt fails
    with pytest.raises(aps.SettingsWriteError):
        aps.write_settings(lambda st: st.setValue("pref/k", "rejected"))
    os.chmod(store, RW)
    aps.write_settings(lambda st: st.setValue("pref/unrelated", "2"))
    text = store.read_text(encoding="utf-8")
    assert "rejected" not in text
    assert "unrelated=2" in text and "other=1" in text
    assert ("k=old" in text) == bool(prior)
    assert aps.app_settings().value("pref/k") == ("old" if prior else None)


def test_a_rejected_removal_and_group_change_are_reconciled(store, attempts):
    """A change made inside a group (save_pedal_cal) and a removal: every
    touched key goes back to what was loaded."""
    from openobd.app import save_pedal_cal
    store.write_text(f"[pedal_cal]\n{VIN}\\rest=18\n{VIN}\\full=65\n"
                     f"{VIN}\\source=stored\n[pref]\ngone=1\n",
                     encoding="utf-8")
    os.chmod(store, stat.S_IREAD)
    with pytest.raises(aps.SettingsWriteError):
        save_pedal_cal(VIN, PedalCal(25.0, None, "rejected"))
    with pytest.raises(aps.SettingsWriteError):
        aps.write_settings(lambda st: st.remove("pref/gone"))
    os.chmod(store, RW)
    aps.write_settings(lambda st: st.setValue("pref/unrelated", "2"))
    text = store.read_text(encoding="utf-8")
    assert "rejected" not in text and "25" not in text
    assert "rest=18" in text and "full=65" in text and "gone=1" in text


@pytest.mark.skipif(not WIN, reason="Windows share-mode semantics")
def test_a_rejected_value_under_an_exclusive_hold_never_reaches_disk(
        store, attempts):
    """Same, with the store held exclusively (a scanner) rather than read
    only. The store is loaded first, so the attempts reuse Qt's parked
    QConfFile and the change is applied and rejected, not skipped."""
    store.write_text("[pref]\nother=1\nk=old\n", encoding="utf-8")
    assert aps.read_settings(lambda st: st.value("pref/k")) == "old"
    close = _hold_exclusive(store)
    try:
        applied = []

        def apply(st):
            applied.append(st.fileName())
            st.setValue("pref/k", "rejected")
        with pytest.raises(aps.SettingsWriteError, match="AccessError"):
            aps.write_settings(apply)
    finally:
        close()
    assert applied                              # really applied, then failed
    aps.write_settings(lambda st: st.setValue("pref/unrelated", "2"))
    text = store.read_text(encoding="utf-8")
    assert "rejected" not in text and "k=old" in text
    assert "unrelated=2" in text


def test_a_change_that_raises_leaves_nothing_pending(store, attempts):
    store.write_text("[pref]\nother=1\n", encoding="utf-8")

    def broken(st):
        st.setValue("pref/half", "applied")
        raise RuntimeError("defect mid-change")
    with pytest.raises(RuntimeError):
        aps.write_settings(broken)
    aps.write_settings(lambda st: st.setValue("pref/unrelated", "2"))
    text = store.read_text(encoding="utf-8")
    assert "half" not in text and "unrelated=2" in text


# --------------------------------------------------------------------------- #
# save_pref failures are surfaced through ONE rate-limited hook
# --------------------------------------------------------------------------- #
@pytest.fixture
def pref_notices(monkeypatch):
    seen: list[tuple[str, str]] = []
    clock = [1000.0]
    monkeypatch.setattr(aps.time, "monotonic", lambda: clock[0])
    aps.set_on_pref_write_failed(lambda key, why: seen.append((key, why)))
    yield seen, clock
    aps.set_on_pref_write_failed(None)


def test_a_failed_pref_is_surfaced_once_per_interval_per_key(
        store, attempts, pref_notices):
    seen, clock = pref_notices
    _read_only_empty(store)
    for _ in range(3):                          # a splitter being dragged
        assert aps.save_pref(None, "diag/split", "a") is False
    assert aps.save_pref(None, "tuning/recent", "[]") is False
    assert [k for k, _w in seen] == ["diag/split", "tuning/recent"]
    assert seen[0][1].startswith("AccessError writing ")
    assert "not saved" not in seen[0][1]        # the bare reason
    clock[0] += aps.PREF_FAILURE_NOTICE_INTERVAL_S
    assert aps.save_pref(None, "diag/split", "a") is False
    assert [k for k, _w in seen] == ["diag/split", "tuning/recent",
                                     "diag/split"]


def test_a_failing_notice_handler_never_raises_into_the_caller(
        store, attempts, caplog):
    def broken(key, why):
        raise RuntimeError("window already deleted")
    aps.set_on_pref_write_failed(broken)
    try:
        _read_only_empty(store)
        with caplog.at_level("ERROR", logger="openobd.appsettings"):
            assert aps.save_pref(None, "diag/split", "a") is False
        assert "preference-failure handler failed" in caplog.text
    finally:
        aps.set_on_pref_write_failed(None)


def test_no_notice_when_the_pref_is_saved(store, pref_notices):
    seen, _clock = pref_notices
    assert aps.save_pref(None, "diag/split", "a") is True
    assert seen == []


@pytest.fixture
def window(store):
    from openobd.app import MainWindow, load_default_cal
    cal, _path = load_default_cal()
    win = MainWindow(cal, None)
    yield win
    win.dirty = False
    win.close()
    assert aps.on_pref_write_failed is None     # closeEvent uninstalls it


def test_the_main_window_shows_a_pref_that_was_not_saved(window, store,
                                                         attempts):
    assert aps.on_pref_write_failed == window._on_pref_write_failed
    _read_only_empty(store)
    assert aps.save_pref(None, "livedata/split", "x") is False
    msg = window.statusBar().currentMessage()
    assert msg.startswith("Preference not saved: livedata/split")
    assert "AccessError" in msg


def test_a_dashboard_style_that_was_not_saved_says_so(window, monkeypatch):
    import openobd.app as appmod
    monkeypatch.setattr(appmod.QDialog, "exec",
                        lambda self: appmod.QDialog.Accepted)
    monkeypatch.setattr(appmod, "save_pref", lambda *_a: False)
    window._open_settings()
    msg = window.statusBar().currentMessage()
    assert "NOT SAVED" in msg and "dashboard/style" in msg
    monkeypatch.setattr(appmod, "save_pref", lambda *_a: True)
    window._open_settings()
    msg = window.statusBar().currentMessage()
    assert msg.startswith("Dashboard visual: ") and "NOT SAVED" not in msg


# --------------------------------------------------------------------------- #
# SC-01: no QSettings write or construction outside openobd/appsettings.py
# --------------------------------------------------------------------------- #
OPENOBD = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "openobd")
_WRITE_METHODS = {"setValue", "remove", "sync"}
#: Receivers taken as QSettings by NAME (the names the code base uses for a
#: settings object: self.settings, self._settings, an apply callback's st).
_SETTINGS_NAMES = {"settings", "_settings", "st"}


def _terminal_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_settings_ctor(node) -> bool:
    return (isinstance(node, ast.Call)
            and _terminal_name(node.func) in ("QSettings", "app_settings"))


def settings_write_violations(source: str, filename: str = "<src>") -> list:
    """Bare QSettings writes / constructions in `source`.

    Heuristic (documented so a reader knows what it can and cannot see):
    * a call .setValue / .remove / .sync is a QSettings write when its
      receiver's terminal name is in _SETTINGS_NAMES, OR was bound in this
      file from QSettings(...) / app_settings(), OR is annotated QSettings
      (parameter, variable or attribute). So dict.remove / list.remove /
      QScrollBar.setValue on other names are not flagged; a QSettings held
      under an unrelated, unannotated name that was never bound from a
      constructor in the same file would be missed.
    * a call inside a function (by name) or lambda passed as the change to
      write_settings(...) is the sanctioned path, not a violation.
    * any QSettings(...) construction is a violation (app_settings() is the
      one constructor).
    """
    tree = ast.parse(source, filename)
    names = set(_SETTINGS_NAMES)
    sanctioned_fns: set[str] = set()
    sanctioned_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            typed = (isinstance(node, ast.AnnAssign)
                     and "QSettings" in ast.unparse(node.annotation))
            if typed or (value is not None and _is_settings_ctor(value)):
                names.update(_terminal_name(t) for t in targets)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            if "QSettings" in ast.unparse(node.annotation):
                names.add(node.arg)
        elif (isinstance(node, ast.Call)
              and _terminal_name(node.func) == "write_settings"):
            change = (node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg == "apply"), None))
            if isinstance(change, ast.Name):
                sanctioned_fns.add(change.id)
            elif isinstance(change, ast.Lambda):
                sanctioned_nodes.update(id(n) for n in ast.walk(change))
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in sanctioned_fns):
            sanctioned_nodes.update(id(n) for n in ast.walk(node))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in sanctioned_nodes:
            continue
        func = node.func
        if _terminal_name(func) == "QSettings":
            found.append(f"{filename}:{node.lineno}: QSettings(...) "
                         f"constructed outside appsettings")
        elif (isinstance(func, ast.Attribute) and func.attr in _WRITE_METHODS
              and _terminal_name(func.value) in names):
            found.append(f"{filename}:{node.lineno}: bare "
                         f"{ast.unparse(func)}(...) outside appsettings")
    return found


@pytest.mark.parametrize("src, bad", [
    ("self.settings.setValue('k', 1)\n", True),
    ("self._settings.remove('k')\n", True),
    ("settings.sync()\n", True),
    ("prefs = app_settings()\nprefs.setValue('k', 1)\n", True),
    ("def f(store: QSettings):\n    store.remove('k')\n", True),
    ("s = QSettings('a', 'b')\n", True),
    ("x = QtCore.QSettings(path, QSettings.IniFormat)\n", True),
    ("lane.remove(name)\nbar.setValue(3)\nd.sync()\n", False),
    ("QSettings.setDefaultFormat(QSettings.IniFormat)\n", False),
    ("def apply(st):\n    st.setValue('k', 1)\n    st.remove('j')\n"
     "write_settings(apply, what='x')\n", False),
    ("write_settings(lambda st: st.setValue('k', 1))\n", False),
    ("def apply(st):\n    st.setValue('k', 1)\n", True),   # never passed
])
def test_the_settings_write_scan_flags_exactly_bare_writes(src, bad):
    assert bool(settings_write_violations(src)) is bad


def test_no_qsettings_write_or_construction_outside_appsettings():
    """SC-01, derived from the tree: every write goes through
    appsettings.write_settings / save_pref, every store through
    app_settings()."""
    found, scanned = [], 0
    for root, _dirs, files in os.walk(OPENOBD):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            if os.path.normcase(path) == os.path.normcase(
                    os.path.join(OPENOBD, "appsettings.py")):
                continue
            with open(path, encoding="utf-8") as fh:
                found += settings_write_violations(
                    fh.read(), os.path.relpath(path, OPENOBD))
            scanned += 1
    assert scanned > 10                         # the scan really ran
    assert found == []
