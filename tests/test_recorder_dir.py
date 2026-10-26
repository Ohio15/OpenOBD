"""Configurable log folder (v0.39.0): Settings → Log folder… stores QSettings
logs/dir; the next recording goes there when it passes a real write probe,
else to the default with an honest "Log folder unavailable" status — never
a silent fallback. Offscreen, a throwaway QSettings INI tree per module and
tmp_path folders only (no real Documents, no real settings store)."""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd.recorder import (  # noqa: E402
    LOG_DIR_KEY, log_dir, log_dir_unavailable, probe_log_dir,
    resolve_log_dir, used_configured_dir,
)
from tests.test_record_metadata import FakeGt  # noqa: E402


@pytest.fixture(scope="module")
def qapp(tmp_path_factory):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(tmp_path_factory.mktemp("logdir_settings")))
    return QApplication.instance() or QApplication([])


def _set_dir(value):
    from openobd.appsettings import app_settings
    st = app_settings()
    if value is None:
        st.remove(LOG_DIR_KEY)
    else:
        st.setValue(LOG_DIR_KEY, value)
    st.sync()


def _blocked(tmp_path):
    """A folder that cannot exist: its parent is a regular file."""
    f = tmp_path / "afile"
    f.write_text("x")
    return str(f / "Logs")


@pytest.fixture
def dash(qapp, tmp_path):
    from openobd.app import Dashboard
    from openobd.appsettings import app_settings
    app_settings().clear()
    d = Dashboard()
    d._record_dirs = lambda: [str(tmp_path / "Default")]
    src = FakeGt()
    d.bind_source(src)
    yield d, src
    if d.recording:
        d._finalize_record()
    d.timer.stop()
    app_settings().clear()


# -- pure resolution ---------------------------------------------------------- #
def test_unset_resolves_to_the_defaults(tmp_path):
    defaults = [str(tmp_path / "a"), str(tmp_path / "b")]
    assert log_dir("", defaults) == (defaults, None)
    assert log_dir(None, defaults) == (defaults, None)


def test_writable_configured_folder_goes_first_and_is_created(tmp_path):
    cfg = str(tmp_path / "deep" / "GTPro")
    dirs, problem = log_dir(cfg, [str(tmp_path / "d")])
    assert problem is None and dirs[0] == cfg
    assert os.path.isdir(cfg)
    assert os.listdir(cfg) == []          # the probe file is deleted


def test_unwritable_configured_folder_is_skipped_with_a_reason(tmp_path):
    cfg = _blocked(tmp_path)
    defaults = [str(tmp_path / "d")]
    dirs, problem = log_dir(cfg, defaults)
    assert dirs == defaults
    assert problem.startswith(log_dir_unavailable(cfg))
    assert problem.startswith(f"Log folder unavailable: {cfg} (using default)")
    assert probe_log_dir(cfg) is not None


# -- Dashboard ---------------------------------------------------------------- #
def test_setting_is_honoured_and_named_in_tooltip_and_status(dash, tmp_path):
    d, src = dash
    cfg = str(tmp_path / "GTPro")
    _set_dir(cfg)
    d.toggle_record()
    assert d.recording
    path = d._recorder.path
    assert os.path.dirname(path) == cfg
    assert "unavailable" not in d.status.text()
    src.feed(1.0, 1, rpm=700.0, vss=0.0, app=19.2)
    d._tick()
    d._finalize_record()
    assert f"log folder: {cfg}" in d.status.text()
    assert f"Log folder: {cfg}" in d._record_tip
    assert os.path.isfile(path)


def test_unset_records_to_the_default(dash, tmp_path):
    d, _src = dash
    _set_dir(None)
    d.toggle_record()
    assert os.path.dirname(d._recorder.path) == str(tmp_path / "Default")
    assert d._rec_dir_note is None


def test_unwritable_setting_falls_back_honestly(dash, tmp_path):
    d, src = dash
    cfg = _blocked(tmp_path)
    _set_dir(cfg)
    d.toggle_record()
    assert d.recording
    assert os.path.dirname(d._recorder.path) == str(tmp_path / "Default")
    banner = f"Log folder unavailable: {cfg} (using default)"
    assert banner in d.status.text()
    assert banner in d._record_tip
    src.feed(1.0, 1, rpm=700.0, vss=0.0, app=19.2)
    d._tick()
    d._finalize_record()
    assert banner in d.status.text() and "Saved 1 samples" in d.status.text()
    assert banner in d._record_tip


def test_markup_in_the_folder_renders_literally(dash, tmp_path):
    # A LEGAL Windows name that reads as markup/entities: the recording must
    # really land there (success path) and the tooltip show it literally.
    from openobd.tuningui import plain_tooltip
    d, _src = dash
    cfg = str(tmp_path / "a&b &lt;i&gt; x&amp;y")
    _set_dir(cfg)
    d.toggle_record()
    assert d.recording and d._rec_dir_note is None
    assert os.path.dirname(d._recorder.path) == cfg
    tip = d.btn_record.toolTip()
    assert tip == plain_tooltip(d._record_tip)
    assert "a&amp;b &amp;lt;i&amp;gt; x&amp;amp;y" in tip
    assert "<i>" not in tip


def test_illegal_folder_name_fails_honestly_and_renders_literally(
        dash, tmp_path):
    # "<" is illegal in a Windows name: the folder cannot be used, the
    # recording goes to the default, and the reason renders literally.
    from openobd.tuningui import plain_tooltip
    d, _src = dash
    cfg = str(tmp_path / "<b>x&amp")
    _set_dir(cfg)
    d.toggle_record()
    if sys.platform == "win32":
        assert os.path.dirname(d._recorder.path) == str(tmp_path / "Default")
        assert d._rec_dir_note.startswith(log_dir_unavailable(cfg))
    tip = d.btn_record.toolTip()
    assert tip == plain_tooltip(d._record_tip)
    assert "<b>" not in tip and "&lt;b&gt;x&amp;amp" in tip


# -- R5: env vars, relative paths, probe hygiene ------------------------------ #
def test_env_vars_in_the_setting_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENOBD_TEST_LOGROOT", str(tmp_path / "root"))
    raw = os.path.join("%OPENOBD_TEST_LOGROOT%", "Logs") \
        if sys.platform == "win32" else "$OPENOBD_TEST_LOGROOT/Logs"
    dirs, problem = log_dir(raw, [str(tmp_path / "d")])
    want = str(tmp_path / "root" / "Logs")
    assert problem is None and dirs[0] == want
    assert os.path.isdir(want)
    assert resolve_log_dir(raw) == (want, None)
    assert used_configured_dir(raw, os.path.join(want, "x.csv"))


def test_relative_or_unexpanded_setting_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    defaults = [str(tmp_path / "d")]
    for raw in ("Logs", os.path.join("%OPENOBD_NO_SUCH_VAR_X%", "Logs")):
        dirs, problem = log_dir(raw, defaults)
        assert dirs == defaults
        assert problem == (f"Log folder must be an absolute path: {raw} "
                           "(using default)")
        assert not used_configured_dir(raw, os.path.join(raw, "x.csv"))
    assert os.listdir(tmp_path) == []            # nothing created under CWD


# Round 2: an empty variable leaves a rooted-but-driveless path ("\\Logs"),
# which Python <= 3.12 calls absolute on Windows; it must be refused too.
def test_empty_env_var_leaving_a_driveless_path_is_refused(tmp_path,
                                                           monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENOBD_TEST_EMPTY", "")
    raw = (os.path.join("%OPENOBD_TEST_EMPTY%", "Logs")
           if sys.platform == "win32" else "$OPENOBD_TEST_EMPTY/Logs")
    defaults = [str(tmp_path / "d")]
    if sys.platform == "win32":
        folder, problem = resolve_log_dir(raw)
        assert folder is None
        assert problem == (f"Log folder must be an absolute path: {raw} "
                           "(using default)")
        dirs, problem = log_dir(raw, defaults)
        assert dirs == defaults and problem is not None
        assert not used_configured_dir(raw, os.path.join("\\Logs", "x.csv"))
        # Python 3.13 already calls "\Logs" relative; emulate the <= 3.12
        # ntpath.isabs (rooted == absolute) so the drive guard itself is
        # exercised on every interpreter.
        import ntpath
        monkeypatch.setattr(ntpath, "isabs",
                            lambda p: ntpath.splitroot(p)[1] != "")
        assert os.path.isabs("\\Logs")
        assert resolve_log_dir(raw)[0] is None
        assert resolve_log_dir("C:\\Logs") == ("C:\\Logs", None)
        monkeypatch.undo()
        # A UNC prefix still counts as a drive.
        assert resolve_log_dir(r"\\server\share\Logs") == (
            r"\\server\share\Logs", None)
    else:
        # POSIX: "/Logs" is genuinely absolute; isabs is sufficient there.
        assert resolve_log_dir(raw) == ("/Logs", None)


def test_relative_setting_is_reported_on_record(dash, tmp_path, monkeypatch):
    d, _src = dash
    monkeypatch.chdir(tmp_path)
    _set_dir("Logs")
    d.toggle_record()
    assert d.recording
    assert os.path.dirname(d._recorder.path) == str(tmp_path / "Default")
    assert "Log folder must be an absolute path: Logs (using default)" \
        in d.status.text()
    assert not (tmp_path / "Logs").exists()


def test_probe_file_removed_when_the_write_fails(tmp_path, monkeypatch):
    import builtins
    import openobd.recorder as R
    monkeypatch.setattr(R, "_probe_name", lambda: ".probe_fixed")
    real_open = builtins.open

    class Failing:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()
            return False

        def write(self, _data):
            raise OSError(28, "No space left on device")

    def fake_open(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)
        if str(path).endswith(".probe_fixed"):
            return Failing(fh)
        return fh
    monkeypatch.setattr(builtins, "open", fake_open)
    why = probe_log_dir(str(tmp_path))
    assert why and "No space left on device" in why
    assert not (tmp_path / ".probe_fixed").exists()


def test_probe_refuses_an_existing_file_at_its_name(tmp_path, monkeypatch):
    import openobd.recorder as R
    monkeypatch.setattr(R, "_probe_name", lambda: ".probe_planted")
    planted = tmp_path / ".probe_planted"
    planted.write_text("not yours")
    why = probe_log_dir(str(tmp_path))
    assert why and why.startswith("FileExistsError")
    assert planted.read_text() == "not yours"    # neither written nor removed


def test_probe_refuses_a_planted_symlink(tmp_path, monkeypatch):
    import openobd.recorder as R
    target = tmp_path / "target.txt"
    target.write_text("keep")
    link = tmp_path / ".probe_link"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    monkeypatch.setattr(R, "_probe_name", lambda: ".probe_link")
    why = probe_log_dir(str(tmp_path))
    assert why and why.startswith("FileExistsError")
    assert target.read_text() == "keep" and os.path.islink(link)


def test_probe_name_is_unpredictable():
    from openobd.recorder import _probe_name
    a, b = _probe_name(), _probe_name()
    assert a != b and a.startswith(f".openobd_write_probe_{os.getpid()}_")


# -- R7: a stalled folder cannot hang the caller ------------------------------ #
def test_stalled_probe_times_out_and_uses_the_default(tmp_path, monkeypatch):
    import threading
    import time
    import openobd.recorder as R
    release = threading.Event()
    monkeypatch.setattr(R, "PROBE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(R, "_probe_write", lambda folder: release.wait(10))
    try:
        t0 = time.monotonic()
        why = probe_log_dir(str(tmp_path / "cloud"))
        assert time.monotonic() - t0 < 2.0
        assert why == "timed out after 0.3 s"
        cfg = str(tmp_path / "cloud")
        dirs, problem = log_dir(cfg, [str(tmp_path / "d")])
        assert dirs == [str(tmp_path / "d")]
        assert problem == (f"{log_dir_unavailable(cfg)} — "
                           "timed out after 0.3 s")
    finally:
        release.set()


def test_stalled_probe_on_record_falls_back_honestly(dash, tmp_path,
                                                     monkeypatch):
    import threading
    import openobd.recorder as R
    d, _src = dash
    release = threading.Event()
    monkeypatch.setattr(R, "PROBE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(R, "_probe_write", lambda folder: release.wait(10))
    cfg = str(tmp_path / "cloud")
    _set_dir(cfg)
    try:
        d.toggle_record()
        assert d.recording
        assert os.path.dirname(d._recorder.path) == str(tmp_path / "Default")
        assert (f"Log folder unavailable: {cfg} (using default) — timed out"
                in d.status.text())
    finally:
        release.set()


def test_stalled_probe_refuses_the_choice(win, monkeypatch, tmp_path):
    import threading
    import openobd.recorder as R
    from openobd.app import QFileDialog, QMessageBox, configured_log_dir
    release = threading.Event()
    monkeypatch.setattr(R, "PROBE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(R, "_probe_write", lambda folder: release.wait(10))
    warned = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(tmp_path / "cloud")))
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a)))
    try:
        assert not win.choose_log_folder()
        assert warned and "timed out after 0.3 s" in warned[0][2]
        assert configured_log_dir() == ""
    finally:
        release.set()


# -- Settings UI -------------------------------------------------------------- #
@pytest.fixture
def win(qapp):
    import openobd.app as A
    from openobd.appsettings import app_settings
    app_settings().clear()
    cal, path = A.load_default_cal()
    w = A.MainWindow(cal, path)
    yield w
    w.dirty = False
    w.dashboard.shutdown()
    app_settings().clear()


def _menu_action(win, text):
    for menu_act in win.menuBar().actions():
        if menu_act.text() == "&Settings":
            for a in menu_act.menu().actions():
                if a.text() == text:
                    return a
    raise AssertionError(f"Settings → {text} not found")


def test_dialog_cancel_leaves_the_setting_unchanged(win, monkeypatch, tmp_path):
    from openobd.app import QFileDialog, configured_log_dir
    _set_dir(str(tmp_path / "kept"))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: ""))
    _menu_action(win, "Log folder…").trigger()
    assert configured_log_dir() == str(tmp_path / "kept")


def test_choose_creates_and_stores_then_reset(win, monkeypatch, tmp_path):
    from openobd.app import QFileDialog, configured_log_dir
    chosen = str(tmp_path / "new" / "GTPro")
    seen = {}

    def pick(parent, title, start):
        seen["start"] = start
        return chosen
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(pick))
    assert win.choose_log_folder()
    assert configured_log_dir() == os.path.normpath(chosen)
    assert os.path.isdir(chosen)
    assert win.log_folder_text() == os.path.normpath(chosen)
    # Load log opens in the configured folder.
    assert win._log_browse_dir() == os.path.normpath(chosen)
    _menu_action(win, "Reset log folder to default").trigger()
    assert configured_log_dir() == ""
    assert win.log_folder_text().endswith("(default)")


def test_unwritable_choice_is_refused(win, monkeypatch, tmp_path):
    from openobd.app import QFileDialog, QMessageBox, configured_log_dir
    bad = _blocked(tmp_path)
    warned = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: bad))
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warned.append(a)))
    assert not win.choose_log_folder()
    assert warned and configured_log_dir() == ""


def test_preferences_row_shows_the_folder_as_plain_text(win, tmp_path):
    from PySide6.QtCore import Qt
    cfg = str(tmp_path / "<i>logs</i>")
    _set_dir(cfg)
    row = win._log_folder_row()
    from openobd.tuningui import ElidedLabel
    label = row.findChild(ElidedLabel, "logFolderLabel")
    assert label.full_text() == cfg
    assert label.textFormat() == Qt.PlainText
    assert "<i>" not in label.toolTip()


# Round 2: Settings shows (and the chooser starts in) the folder the setting
# resolves to, never the raw value; an unusable value shows the default plus
# why.
def test_settings_text_and_chooser_use_the_resolved_folder(win, monkeypatch,
                                                           tmp_path):
    from openobd.app import QFileDialog
    monkeypatch.setenv("OPENOBD_TEST_LOGROOT2", str(tmp_path / "root"))
    raw = (os.path.join("%OPENOBD_TEST_LOGROOT2%", "Logs")
           if sys.platform == "win32" else "$OPENOBD_TEST_LOGROOT2/Logs")
    want = str(tmp_path / "root" / "Logs")
    os.makedirs(want)
    _set_dir(raw)
    assert win.log_folder_text() == want
    assert "(default)" not in win.log_folder_text()
    assert win._log_browse_dir() == want
    seen = {}

    def pick(parent, title, start):
        seen["start"] = start
        return ""
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(pick))
    assert not win.choose_log_folder()
    assert seen["start"] == want


def test_settings_text_for_an_unusable_setting_shows_default_and_why(
        win, monkeypatch, tmp_path):
    from openobd.app import QFileDialog
    monkeypatch.chdir(tmp_path)
    _set_dir("Logs")
    defaults = win.dashboard._record_dirs()
    text = win.log_folder_text()
    assert text.startswith(f"{defaults[0]} (default)")
    assert ("Log folder must be an absolute path: Logs (using default)"
            in text)
    seen = {}

    def pick(parent, title, start):
        seen["start"] = start
        return ""
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(pick))
    assert not win.choose_log_folder()
    assert seen["start"] == defaults[0]
    assert not (tmp_path / "Logs").exists()
