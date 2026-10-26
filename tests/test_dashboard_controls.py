"""
v0.34.0 Dashboard controls: GT auto-connect, the big Record/Stop button,
Mark beside it while recording, Start/Stop in the replay strip, the truck-mcp
session picker in the File menu, and the R / M / Space keys.

No hardware and no wall-clock waits: the GT's arrival is a fake ports
provider, the connect worker builds a fake GtDataSource, scan ticks are
driven by hand (the QTimer interval is set out of reach), and recording time
runs on the fake source's virtual clock.
"""
from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd.transport import DataSource, GtDataSource, Sample  # noqa: E402

NEVER_MS = 3_600_000       # the scan timer never fires inside a test


@pytest.fixture(scope="module")
def qapp(tmp_path_factory):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(tmp_path_factory.mktemp("controls_settings")))
    return QApplication.instance() or QApplication([])


class FakeGt(GtDataSource):
    """A GtDataSource whose start() opens nothing: 'connected' means a poll
    thread is marked running. Time is the test's virtual clock (now)."""

    def __init__(self):
        super().__init__(port="COM7")
        self._gt = SimpleNamespace(
            close=lambda: None, supported=set(), port_name="COM7",
            possible_keys=lambda: ["rpm", "vss", "voltage"])
        self.now = 0.0
        self.stopped = False

    def start(self):
        self._t0 = 0.0
        self.port_name = "COM7"
        self._thread = object()       # looks like a running poll thread

    def stop(self):
        self._thread = None
        self.stopped = True

    def clock(self):
        return self.now

    def feed(self, t, **vals):
        s = Sample(t, vals)
        self._latest = s
        self._queue.append(s)
        self.now = t


class Factory:
    """The connect worker's source factory: counts builds, can fail the
    next build, and can hold start() until the test releases it (a connect
    'in flight')."""

    def __init__(self):
        self.built: list[FakeGt] = []
        self.fail_next: str | None = None
        self.gate: threading.Event | None = None

    def __call__(self):
        if self.fail_next:
            msg, self.fail_next = self.fail_next, None
            raise OSError(msg)
        src = FakeGt()
        gate = self.gate
        if gate is not None:
            real_start = src.start

            def held_start():
                gate.wait(5.0)        # bounded: a broken test cannot hang
                real_start()
            src.start = held_start
        self.built.append(src)
        return src


class Ports:
    """Fake list_ports: what the scan sees, and how often it looked."""

    def __init__(self):
        self.present: list[str] = []
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return list(self.present)


class StoreSource(DataSource):
    """A bound truck-mcp session stand-in (archived, not seekable)."""
    is_store_record = True
    provenance = "simulated"

    def channels(self):
        return ["rpm"]


class LiveFollowSource(DataSource):
    """A non-seekable, non-GT source (truck-mcp live follow shape)."""

    def channels(self):
        return ["rpm"]


class ReplaySource(DataSource):
    """A seekable replay stand-in."""
    speed = 1.0
    playing = False

    def channels(self):
        return ["rpm"]

    def duration(self):
        return 10.0

    def position(self):
        return 0.0

    def seek(self, t):
        pass

    def pause(self):
        self.playing = False

    def resume(self):
        self.playing = True


def _settle(app):
    """Let the connect / release workers finish (join, not sleep) and
    deliver their queued signals to the GUI thread."""
    for t in list(threading.enumerate()):
        if t.name in ("gt-connect", "gt-release"):
            t.join(5.0)
    app.processEvents()


@pytest.fixture
def rig(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from openobd.app import Dashboard
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.No)
    ports, factory = Ports(), Factory()
    d = Dashboard(ports_provider=ports, scan_interval_ms=NEVER_MS,
                  gt_factory=factory)
    d._record_dirs = lambda: [str(tmp_path / "Logs")]
    yield SimpleNamespace(d=d, ports=ports, factory=factory, app=qapp)
    if factory.gate is not None:
        factory.gate.set()
    _settle(qapp)
    d.shutdown()


# --------------------------------------------------------------------------- #
# auto-connect
# --------------------------------------------------------------------------- #
def test_scan_is_injectable_and_runs_at_creation(rig):
    from openobd.app import LOOKING_FOR_GT
    d = rig.d
    assert d._scan_timer.interval() == NEVER_MS
    assert d._scan_timer.isActive()
    assert rig.ports.calls == 1                      # scanned at creation
    assert d.status.text() == LOOKING_FOR_GT
    assert "R record" in d.status.toolTip() and "Space" in d.status.toolTip()


def test_auto_connect_fires_when_the_gt_appears_and_not_before(rig):
    d, ports, factory = rig.d, rig.ports, rig.factory
    for _ in range(3):
        d._scan_for_gt()
    assert factory.built == [] and d.source is None and not d._gt_connecting
    ports.present = ["COM7"]
    d._scan_for_gt()
    assert d._gt_connecting
    _settle(rig.app)
    assert len(factory.built) == 1 and d.source is factory.built[0]
    assert d._gt_live() and not d._gt_connecting
    assert "OBDX Pro GT live" in d.status.text()
    looked = ports.calls
    d._scan_for_gt()                                 # live GT: no scan
    assert ports.calls == looked and len(factory.built) == 1


def test_no_scan_while_a_connect_is_in_flight(rig):
    d, ports, factory = rig.d, rig.ports, rig.factory
    factory.gate = threading.Event()
    ports.present = ["COM7"]
    d._scan_for_gt()
    assert d._gt_connecting
    looked = ports.calls
    d._scan_for_gt()
    d.connect_gt(auto=True)                          # a second open: refused
    assert ports.calls == looked
    factory.gate.set()
    _settle(rig.app)
    assert len(factory.built) == 1 and d._gt_live()


@pytest.mark.parametrize("source_cls", [StoreSource, ReplaySource,
                                        LiveFollowSource])
def test_no_scan_while_the_user_bound_another_source(rig, source_cls):
    d, ports, factory = rig.d, rig.ports, rig.factory
    d.bind_source(source_cls())
    ports.present = ["COM7"]
    looked = ports.calls
    for _ in range(3):
        d._scan_for_gt()
    assert ports.calls == looked and factory.built == []
    d._teardown_source()                             # unbound: scan resumes
    d._scan_for_gt()
    assert ports.calls == looked + 1
    _settle(rig.app)
    assert d._gt_live()


def test_a_failed_connect_is_reported_and_retried_on_the_next_tick(rig):
    d, ports, factory = rig.d, rig.ports, rig.factory
    factory.fail_next = "could not open port 'COM7': Access is denied"
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    assert d.source is None and not d._gt_connecting
    assert "GT connect failed" in d.status.text()
    assert "Access is denied" in d.status.text()
    assert "retrying" in d.status.text()
    d._scan_for_gt()                                 # next tick: retry
    _settle(rig.app)
    assert d._gt_live() and len(factory.built) == 1


def test_disconnect_suppresses_until_the_port_goes_away_and_comes_back(rig):
    from openobd.app import LOOKING_FOR_GT
    d, ports, factory = rig.d, rig.ports, rig.factory
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    first = d.source
    d.disconnect_gt()
    assert first.stopped and not d._gt_live() and d._auto_connect_suppressed
    assert "GT disconnected" in d.status.text()
    for _ in range(3):                               # still plugged in
        d._scan_for_gt()
    _settle(rig.app)
    assert len(factory.built) == 1 and not d._gt_live()
    assert "GT disconnected" in d.status.text()
    ports.present = []                               # unplugged
    d._scan_for_gt()
    assert not d._auto_connect_suppressed
    assert d.status.text() == LOOKING_FOR_GT
    ports.present = ["COM7"]                         # replugged
    d._scan_for_gt()
    _settle(rig.app)
    assert len(factory.built) == 2 and d.source is factory.built[1]
    assert d._gt_live()


def test_reconnect_now_lifts_a_disconnect(rig):
    d, ports, factory = rig.d, rig.ports, rig.factory
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    d.disconnect_gt()
    d._scan_for_gt()
    _settle(rig.app)
    assert len(factory.built) == 1
    d.reconnect_gt_now()
    _settle(rig.app)
    assert len(factory.built) == 2 and d._gt_live()
    assert not d._auto_connect_suppressed


def test_disconnect_mid_record_finalizes_the_recording(rig, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    d, ports = rig.d, rig.ports
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    src = d.source
    d.toggle_record()
    assert d.recording
    path = d.last_record_path
    src.feed(1.0, rpm=700.0)
    d._tick()
    src.feed(1.5, rpm=710.0)                         # after the last tick
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: pytest.fail("dialog on disconnect"))
    d.disconnect_gt()
    assert not d.recording and d._recorder is None
    with open(path, encoding="utf-8") as fh:
        assert len(fh.read().splitlines()) == 3      # header + 2 rows
    assert f"Saved 2 samples → {path}" in d.status.text()
    assert "GT disconnected" in d.status.text()


def test_an_auto_connect_landing_after_the_user_chose_a_session_is_dropped(rig):
    d, ports, factory = rig.d, rig.ports, rig.factory
    factory.gate = threading.Event()
    ports.present = ["COM7"]
    d._scan_for_gt()
    store = StoreSource()
    d.bind_source(store)                             # user picked a session
    factory.gate.set()
    _settle(rig.app)
    assert d.source is store and factory.built[0].stopped
    assert not d._gt_connecting


def test_disconnect_during_an_auto_connect_drops_it(rig):
    d, ports, factory = rig.d, rig.ports, rig.factory
    factory.gate = threading.Event()
    ports.present = ["COM7"]
    d._scan_for_gt()
    d.disconnect_gt()
    assert "cancelled" in d.status.text()
    factory.gate.set()
    _settle(rig.app)
    assert d.source is None and factory.built[0].stopped


def test_stop_and_start_do_not_touch_a_gt(rig):
    d, ports = rig.d, rig.ports
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    assert not d.btn_stop.isEnabled() and not d.btn_start.isEnabled()
    d.btn_stop.click()                               # Shift+F5: no-op
    assert d._gt_live()
    d.disconnect_gt()
    d.start()                                        # F5 never opens a port
    assert not d._gt_live() and "Reconnect GT now" in d.status.text()


# --------------------------------------------------------------------------- #
# the big Record/Stop button and Mark
# --------------------------------------------------------------------------- #
def test_record_button_labels_and_state(rig):
    from openobd.app import PRIMARY_ROW_HEIGHT, RECORD_IDLE_TEXT
    d, ports = rig.d, rig.ports
    assert d.btn_record.text() == RECORD_IDLE_TEXT
    assert d.btn_record.minimumHeight() == PRIMARY_ROW_HEIGHT
    assert d.btn_record.objectName() == "recordButton"
    assert "QPushButton#recordButton" in d.btn_record.styleSheet()
    assert not d.btn_record.isEnabled()              # no live source
    assert "No live source" in d.btn_record.toolTip()
    assert d.btn_mark.isHidden()
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    src = d.source
    assert d.btn_record.isEnabled() and d.btn_mark.isHidden()
    d.btn_record.click()
    assert d.recording
    assert d.btn_record.text() == "⏹  STOP  ·  0 samples  ·  00:00"
    assert d.btn_record.property("recording") is True
    assert not d.btn_mark.isHidden() and d.btn_mark.isEnabled()
    assert d.btn_mark.minimumHeight() == PRIMARY_ROW_HEIGHT
    for i in range(1234):
        src._queue.append(Sample(10.0 + i * 0.1, {"rpm": 700.0 + i}))
    src.now = 10.0 + 134.5                           # 2 min 14.5 s in
    d._tick()
    assert d.btn_record.text() == "⏹  STOP  ·  1,234 samples  ·  02:14"
    assert d.btn_record.isEnabled()
    d.btn_record.click()                             # interactive stop
    assert not d.recording
    assert d.btn_record.text() == RECORD_IDLE_TEXT
    assert d.btn_record.property("recording") is False
    assert d.btn_mark.isHidden() and not d.btn_mark.isEnabled()
    assert d.btn_record.toolTip().startswith("Last recording: ")
    assert "Saved 1234 samples" in d.status.text()


def test_record_is_disabled_with_the_reason_for_a_truck_mcp_session(rig):
    d = rig.d
    d.bind_source(StoreSource())
    assert not d.btn_record.isEnabled()
    tip = d.btn_record.toolTip()
    assert "archived SIMULATED" in tip and "Settings → Reconnect GT now" in tip
    d.toggle_record()                                # the guard still holds
    assert not d.recording and "Settings → Reconnect GT now" in d.status.text()


def test_marker_needs_a_recording_and_m_still_marks(rig):
    d, ports = rig.d, rig.ports
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    src = d.source
    assert d.shortcut_tooltip(d.btn_mark).endswith("(M)")
    d.toggle_record()
    src.feed(1.0, rpm=700.0)
    d._tick()
    src.now = 3.0
    _shortcut(d, "M").activated.emit()
    assert "mark 1 at 2.0 s" in d.status.text()


def test_elapsed_formatting():
    from openobd.app import format_elapsed, format_record_label
    assert format_elapsed(None) == "00:00"
    assert format_elapsed(-1.0) == "00:00"
    assert format_elapsed(134.9) == "02:14"
    assert format_elapsed(3725.0) == "1:02:05"
    assert format_record_label(1234567, 61.0) == \
        "⏹  STOP  ·  1,234,567 samples  ·  01:01"


# --------------------------------------------------------------------------- #
# replay transport strip
# --------------------------------------------------------------------------- #
def test_start_and_stop_live_only_in_the_replay_strip(rig):
    d, ports = rig.d, rig.ports
    assert d.btn_start.parent() is d.replay_ctl
    assert d.btn_stop.parent() is d.replay_ctl
    assert d.btn_record.parent() is d and d.btn_mark.parent() is d
    assert d.replay_ctl.isHidden()                   # nothing bound
    rp = ReplaySource()
    d.bind_source(rp)
    assert not d.replay_ctl.isHidden()
    for w in (d.btn_start, d.btn_stop, d.btn_pause, d.speed_combo,
              d.pos_slider):
        assert not w.isHidden(), w
    d.bind_source(LiveFollowSource())                # not seekable
    assert not d.replay_ctl.isHidden()
    assert not d.btn_start.isHidden() and not d.btn_stop.isHidden()
    assert d.btn_pause.isHidden() and d.pos_slider.isHidden()
    d._teardown_source()
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    assert d._gt_live() and d.replay_ctl.isHidden()  # the GT has no Stop


def test_space_toggles_pause_on_a_replay(rig):
    d = rig.d
    rp = ReplaySource()
    rp.playing = True
    d.bind_source(rp)
    _shortcut(d, "Space").activated.emit()
    assert rp.playing is False and d.btn_pause.text().startswith("▶")
    _shortcut(d, "Space").activated.emit()
    assert rp.playing is True


def test_r_toggles_record(rig):
    d = rig.d
    rp = ReplaySource()
    d.bind_source(rp)
    assert d.shortcut_tooltip(d.btn_record).endswith("(R)")
    _shortcut(d, "R").activated.emit()
    assert d.recording
    _shortcut(d, "R").activated.emit()
    assert not d.recording


def _shortcut(d, key):
    from PySide6.QtGui import QKeySequence
    want = QKeySequence(key)
    return next(sc for sc in d._shortcuts if sc.key() == want)


# --------------------------------------------------------------------------- #
# truck-mcp session picker
# --------------------------------------------------------------------------- #
def test_session_picker_tags_simulated_sessions_and_has_a_header(rig):
    from openobd.app import TM_PICKER_HEADER
    d = rig.d
    menu = d._build_tm_menu([
        {"name": "demo", "simulated": True, "path": "a.tmsession.db",
         "started_utc": "2026-10-20T10:00:00Z", "channel_count": 4},
        {"name": "drive", "provenance": "real", "path": "b.tmsession.db",
         "started_utc": "2026-10-21T10:00:00Z", "channel_count": 9},
        {"name": "odd", "provenance": "unknown", "path": "c.tmsession.db"},
    ])
    acts = [a for a in menu.actions() if not a.isSeparator()]
    assert acts[0].text() == TM_PICKER_HEADER and not acts[0].isEnabled()
    assert menu.actions()[1].isSeparator()
    texts = [a.text() for a in acts[1:]]
    assert texts[0].startswith("[SIM] demo")
    assert texts[1].startswith("drive")
    assert texts[2].startswith("[PROVENANCE UNKNOWN] odd")
    assert all(a.isEnabled() for a in acts[1:])
    menu.deleteLater()


def test_session_picker_with_no_sessions_keeps_the_header(rig):
    from openobd.app import TM_PICKER_HEADER
    menu = rig.d._build_tm_menu([])
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts == [TM_PICKER_HEADER, "No drive-log sessions found"]
    menu.deleteLater()


# --------------------------------------------------------------------------- #
# MainWindow: the menu items that replaced the bar buttons
# --------------------------------------------------------------------------- #
def test_main_window_menus_replace_the_connect_and_session_buttons(qapp):
    from openobd.app import (
        DISCONNECT_GT_LABEL, OPEN_TM_SESSION_LABEL, RECONNECT_GT_LABEL,
        MainWindow, load_default_cal)
    cal, path = load_default_cal()
    win = MainWindow(cal, path)
    try:
        d = win.dashboard
        assert not hasattr(d, "btn_connect") and not hasattr(d, "btn_tm")
        menus = {m.title().replace("&", ""): m for m in
                 (a.menu() for a in win.menuBar().actions()) if m}
        file_items = [a.text() for a in menus["File"].actions()]
        assert OPEN_TM_SESSION_LABEL in file_items
        settings = {a.text(): a for a in menus["Settings"].actions()}
        assert settings[RECONNECT_GT_LABEL].shortcut().toString() == "Ctrl+G"
        assert DISCONNECT_GT_LABEL in settings
        settings[DISCONNECT_GT_LABEL].trigger()      # nothing bound
        assert d.status.text() == "No GT is connected."
    finally:
        win.dashboard.shutdown()
        win.deleteLater()
