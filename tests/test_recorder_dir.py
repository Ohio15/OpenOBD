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
    from openobd.tuningui import plain_tooltip
    d, _src = dash
    cfg = str(tmp_path / "<b>x&amp")
    _set_dir(cfg)
    d.toggle_record()
    tip = d.btn_record.toolTip()
    assert tip == plain_tooltip(d._record_tip)
    assert "<b>" not in tip and "&lt;b&gt;x&amp;amp" in tip


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
