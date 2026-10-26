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
from vclock import Clock  # noqa: E402

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
        self.fail_always: str | None = None      # a port held elsewhere
        self.attempts = 0
        self.gate: threading.Event | None = None
        self.cls = FakeGt

    def __call__(self):
        self.attempts += 1
        if self.fail_always:
            raise OSError(self.fail_always)
        if self.fail_next:
            msg, self.fail_next = self.fail_next, None
            raise OSError(msg)
        src = self.cls()
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
    ports, factory, clock = Ports(), Factory(), Clock(start=1000.0)
    d = Dashboard(ports_provider=ports, scan_interval_ms=NEVER_MS,
                  gt_factory=factory, now=clock)
    d._record_dirs = lambda: [str(tmp_path / "Logs")]
    yield SimpleNamespace(d=d, ports=ports, factory=factory, app=qapp,
                          clock=clock)
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


def test_no_second_connect_while_one_is_in_flight(rig):
    d, ports, factory = rig.d, rig.ports, rig.factory
    factory.gate = threading.Event()
    ports.present = ["COM7"]
    d._scan_for_gt()
    assert d._gt_connecting
    d._scan_for_gt()                       # presence checked, no new open
    d.connect_gt(auto=True)                          # a second open: refused
    assert factory.attempts == 1
    factory.gate.set()
    _settle(rig.app)
    assert len(factory.built) == 1 and d._gt_live()


@pytest.mark.parametrize("source_cls", [StoreSource, ReplaySource,
                                        LiveFollowSource])
def test_no_connect_while_the_user_bound_another_source(rig, source_cls):
    from openobd.app import GT_DETECTED_HINT
    d, ports, factory = rig.d, rig.ports, rig.factory
    d.bind_source(source_cls())
    ports.present = ["COM7"]
    for _ in range(3):
        d._scan_for_gt()
    _settle(rig.app)
    assert factory.attempts == 0 and not d._gt_connecting
    assert d.status.text().count(GT_DETECTED_HINT) == 1   # once, not 3x
    d._teardown_source()                             # unbound: scan resumes
    d._scan_for_gt()
    _settle(rig.app)
    assert d._gt_live()


def test_detected_hint_survives_a_replay_stop_and_clears_on_unplug(rig):
    from openobd.app import GT_DETECTED_HINT
    d, ports = rig.d, rig.ports
    d.bind_source(ReplaySource())
    d._scan_for_gt()
    assert GT_DETECTED_HINT not in d.status.text()   # no GT yet
    ports.present = ["COM7"]
    d._scan_for_gt()
    assert d.status.text().endswith(GT_DETECTED_HINT)
    d.stop()                                         # replay Stop: rewrites
    assert d.status.text() == "Stopped."
    d._scan_for_gt()
    assert d.status.text() == f"Stopped. {GT_DETECTED_HINT}"
    ports.present = []                               # GT unplugged
    d._scan_for_gt()
    assert d.status.text() == "Stopped."
    assert rig.factory.attempts == 0


def test_a_failed_open_backs_off_5_15_30_with_one_stable_status_line(rig):
    from openobd.app import GT_RETRY_BACKOFF_S
    d, ports, factory, clock = rig.d, rig.ports, rig.factory, rig.clock
    assert GT_RETRY_BACKOFF_S == (5.0, 15.0, 30.0)
    factory.fail_always = "COM7 is in use by another program"
    ports.present = ["COM7"]
    writes = []
    real_set = d.status.setText
    d.status.setText = lambda text: (writes.append(text), real_set(text))
    line = ("OBDX Pro GT found on COM7 but it could not be opened "
            "(COM7 is in use by another program) · retrying")

    def tick_at(t):
        clock.now = t
        d._scan_for_gt()
        _settle(rig.app)

    tick_at(1000.0)
    assert factory.attempts == 1 and d.status.text() == line
    schedule = []
    t = 1000.0
    for _ in range(5):                    # 2 s ticks, record retry times
        attempts = factory.attempts
        while factory.attempts == attempts:
            t += 2.0
            tick_at(t)
        schedule.append(t)
    gaps = [b - a for a, b in zip([1000.0] + schedule, schedule)]
    assert gaps == [6.0, 16.0, 30.0, 30.0, 30.0]   # 5/15/30 s on 2 s ticks
    assert d.status.text() == line
    assert writes.count(line) == 1, writes        # never rewritten
    assert "Connecting to OBDX Pro GT…" not in writes[1:], writes
    assert d.source is None and not d._gt_connecting
    # the port going away resets the backoff: the replug connects at once
    ports.present = []
    tick_at(t + 2.0)
    factory.fail_always = None
    ports.present = ["COM7"]
    tick_at(t + 4.0)
    assert d._gt_live()


def test_reconnect_now_skips_the_backoff(rig):
    d, ports, factory, clock = rig.d, rig.ports, rig.factory, rig.clock
    factory.fail_next = "Access is denied"
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    d._scan_for_gt()                                 # inside the 5 s wait
    _settle(rig.app)
    assert factory.attempts == 1
    d.reconnect_gt_now()
    _settle(rig.app)
    assert d._gt_live() and factory.attempts == 2


def test_held_port_through_the_real_transport_open(qapp):
    """No factory fake: a real GtDataSource on a port that cannot be opened
    fails in pyserial and lands as the stable 'could not be opened' line."""
    from openobd.app import Dashboard
    ports = Ports()
    ports.present = ["COM7"]
    d = Dashboard(ports_provider=ports, scan_interval_ms=NEVER_MS,
                  gt_factory=lambda: GtDataSource(
                      port="OPENOBD_TEST_NO_SUCH_PORT"),
                  now=Clock(start=0.0))
    try:
        _settle(qapp)                                # creation scan connected
        text = d.status.text()
        assert text.startswith("OBDX Pro GT found on COM7 but it could not "
                               "be opened (") and text.endswith("· retrying")
        assert "OPENOBD_TEST_NO_SUCH_PORT" in text
        assert d.source is None and not d._gt_connecting and d._fail_count == 1
    finally:
        d.shutdown()


def test_connect_watchdog_fails_a_hung_open_and_drops_its_result(rig):
    from openobd.app import GT_CONNECT_TIMEOUT_MS
    d, ports, factory = rig.d, rig.ports, rig.factory
    assert d._connect_watchdog.interval() == GT_CONNECT_TIMEOUT_MS == 20_000
    factory.gate = threading.Event()
    ports.present = ["COM7"]
    d._scan_for_gt()
    assert d._gt_connecting and d._connect_watchdog.isActive()
    d._connect_watchdog.timeout.emit()               # 20 s, on test time
    assert not d._gt_connecting
    assert "could not be opened (no answer within 20 s) · retrying" in \
        d.status.text()
    factory.gate.set()                               # the open finally lands
    _settle(rig.app)
    assert d.source is None and factory.built[0].stopped
    assert d._fail_count == 1 and d._retry_at == rig.clock.now + 5.0


def test_watchdog_stops_once_the_connect_lands(rig):
    d, ports = rig.d, rig.ports
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    assert d._gt_live() and not d._connect_watchdog.isActive()


def test_manual_connect_dropped_when_disconnect_comes_during_the_open(rig):
    d, factory = rig.d, rig.factory
    factory.gate = threading.Event()
    d.reconnect_gt_now()                             # manual, not auto
    assert d._gt_connecting
    d.disconnect_gt()
    assert "cancelled" in d.status.text() and not d._gt_connecting
    factory.gate.set()
    _settle(rig.app)
    assert d.source is None and factory.built[0].stopped
    assert "cancelled" in d.status.text()


def test_manual_connect_dropped_when_another_source_is_bound_meanwhile(rig):
    d, factory = rig.d, rig.factory
    factory.gate = threading.Event()
    d.connect_gt()                                   # manual
    store = StoreSource()
    d.bind_source(store)                             # cancels the connect
    assert not d._gt_connecting
    bound_line = d.status.text()
    factory.gate.set()
    _settle(rig.app)
    assert d.source is store and factory.built[0].stopped
    assert d.status.text() == bound_line             # their status stands


# --------------------------------------------------------------------------- #
# review round 2
# --------------------------------------------------------------------------- #
def test_a_hung_open_blocks_new_workers_until_it_returns(rig):
    from openobd.app import GT_OPEN_HUNG
    d, ports, factory, clock = rig.d, rig.ports, rig.factory, rig.clock
    factory.gate = threading.Event()                 # the OS open hangs
    ports.present = ["COM7"]
    d._scan_for_gt()
    d._connect_watchdog.timeout.emit()               # 20 s on test time
    assert not d._gt_connecting and d._fail_count == 1
    for _ in range(40):                              # 80 s of 2 s ticks
        clock.now += 2.0
        d._scan_for_gt()
    assert factory.attempts == 1                     # no second worker
    assert sum(1 for t in threading.enumerate() if t.name == "gt-connect") == 1
    assert d.status.text() == GT_OPEN_HUNG
    factory.gate.set()                               # the open returns late
    _settle(rig.app)
    assert d.source is None and factory.built[0].stopped
    factory.gate = None
    d._scan_for_gt()                                 # scanning resumes
    _settle(rig.app)
    assert factory.attempts == 2 and d._gt_live()


def test_unplug_during_a_connect_cancels_it_and_resets_the_backoff(rig):
    d, ports, factory, clock = rig.d, rig.ports, rig.factory, rig.clock
    factory.fail_next = "Access is denied"
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    assert d._fail_count == 1
    clock.now += 6.0
    factory.gate = threading.Event()
    d._scan_for_gt()                                 # retry, opening...
    assert d._gt_connecting
    ports.present = []                               # ...unplugged
    d._scan_for_gt()
    assert not d._gt_connecting
    assert d._fail_count == 0 and d._retry_at is None
    factory.gate.set()
    _settle(rig.app)
    assert d.source is None and factory.built[0].stopped
    factory.gate = None
    ports.present = ["COM7"]                         # replug: no backoff
    d._scan_for_gt()
    _settle(rig.app)
    assert d._gt_live() and factory.attempts == 3


def test_a_superseded_connect_never_touches_the_replay_status(rig):
    d, factory = rig.d, rig.factory
    factory.gate = threading.Event()
    d.reconnect_gt_now()                             # manual connect
    d.bind_source(ReplaySource())                    # user loads a replay
    replay_line = d.status.text()
    d._connect_watchdog.timeout.emit()
    d._connect_failed("late failure")                # and a stray failure
    assert d.status.text() == replay_line
    assert d._fail_count == 0 and d._retry_at is None
    factory.gate.set()
    _settle(rig.app)
    assert isinstance(d.source, ReplaySource)
    assert d.status.text() == replay_line


def test_stop_never_closes_the_port_under_a_lock_holder():
    closes = []
    src = GtDataSource(port="COM5")
    src._gt = SimpleNamespace(close=lambda: closes.append(1), supported=set(),
                              port_name="COM5")
    src.stop_lock_wait_s = 0.05            # bounded wait, kept short here
    entered, release = threading.Event(), threading.Event()

    def held(_gt):
        entered.set()
        release.wait(5.0)
        assert closes == [], "port closed underneath the lock holder"

    worker = threading.Thread(target=src.run_exclusive, args=(held,))
    worker.start()
    assert entered.wait(5.0)
    src.stop()                                       # times out on the lock
    assert closes == [] and src._closing
    release.set()
    worker.join(5.0)
    assert closes == [1]                             # closed on release


class CtxGt(FakeGt):
    """The recording-start read is still running when the user stops."""
    VIN = "3GCRKTE35AG150432"

    def __init__(self):
        super().__init__()
        self.stopping = threading.Event()

    def read_recording_context(self):
        self.stopping.wait(5.0)
        return {"epoch": self.link[0],
                "identity": {"examined": True,
                             "vin": {"examined": True, "value": self.VIN}},
                "dtcs_at_start": {"examined": True, "codes": []}}

    def stop(self):
        self.stopping.set()
        super().stop()


def test_stop_waits_for_the_recording_start_read_before_finalizing(rig):
    from openobd.recorder import read_meta
    d, ports, factory = rig.d, rig.ports, rig.factory
    factory.cls = CtxGt
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    src = d.source
    d.toggle_record()
    path = d.last_record_path
    src.feed(1.0, rpm=700.0)
    d._tick()
    d.stop()
    meta = read_meta(path)
    assert meta["vehicle"]["vin"]["value"] == CtxGt.VIN   # not "stopped before"
    assert meta["dtcs_at_start"]["examined"] is True
    rig.app.processEvents()                  # the queued signal: no reapply
    assert read_meta(path)["vehicle"]["vin"]["value"] == CtxGt.VIN


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
    assert d.btn_record.toolTip().startswith("<qt>Last recording: ")
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
    assert d.shortcut_tooltip(d.btn_mark).endswith("(M)</qt>")
    d.toggle_record()
    src.feed(1.0, rpm=700.0)
    d._tick()
    src.now = 3.0
    _shortcut(d, "M").activated.emit()
    assert "mark 1 at 2.0 s" in d.status.text()


class PublishingGt(FakeGt):
    """stop() behaves like GtDataSource.stop(): the poll thread keeps
    publishing until the join returns. Those samples must reach the CSV."""

    def stop(self):
        for k in range(3):
            self.feed(self.now + 0.25, rpm=900.0 + k)
        super().stop()


@pytest.mark.parametrize("path", ["stop", "disconnect", "rebind", "close"])
def test_samples_published_during_stop_land_in_the_csv(rig, monkeypatch, path):
    import csv
    from PySide6.QtWidgets import QMessageBox
    from openobd.app import MainWindow
    d, ports, factory = rig.d, rig.ports, rig.factory
    factory.cls = PublishingGt
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    src = d.source
    assert isinstance(src, PublishingGt) and d._gt_live()
    d.toggle_record()
    path_csv = d.last_record_path
    src.feed(1.0, rpm=700.0)
    d._tick()
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: pytest.fail("dialog on " + path))
    if path == "stop":
        d.stop()
    elif path == "disconnect":
        d.disconnect_gt()
    elif path == "rebind":
        d.bind_source(StoreSource())
    else:
        MainWindow._shutdown_pages(
            SimpleNamespace(dashboard=d, livedata=None, active_tests=None))
    assert src.stopped and not d.recording
    with open(path_csv, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))[1:]
    assert [r[0] for r in rows] == ["0.000", "0.250", "0.500", "0.750"]
    if path in ("stop", "disconnect"):
        assert "Saved 4 samples" in d.status.text()


def test_record_is_disabled_for_a_log_replay(rig):
    from openobd.app import REPLAY_RECORD_REASON
    from openobd.tuningui import plain_tooltip
    d = rig.d
    d.bind_source(ReplaySource())
    assert not d.btn_record.isEnabled()
    assert d.btn_record.toolTip() == plain_tooltip(REPLAY_RECORD_REASON)
    assert REPLAY_RECORD_REASON == "Recording needs a live GT; this is a replay."
    d.toggle_record()                                # R / direct call too
    assert not d.recording and d.status.text() == REPLAY_RECORD_REASON


def test_record_is_disabled_for_a_stopped_gt(rig):
    d, ports = rig.d, rig.ports
    ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    assert d.btn_record.isEnabled()
    d.disconnect_gt()
    assert not d.btn_record.isEnabled()
    assert "The GT is disconnected" in d.btn_record.toolTip()


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
    rig.ports.present = ["COM7"]
    d._scan_for_gt()
    _settle(rig.app)
    assert d.shortcut_tooltip(d.btn_record).endswith("(R)</qt>")
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


def test_record_tooltip_renders_a_hostile_path_literally(rig, monkeypatch):
    """DR-032 868953ce: the record button's tooltip carries a recording path
    or block reason (runtime text); markup in it must not render as HTML,
    including through the shortcut-hint composition shown on hover."""
    from openobd.tuningui import plain_tooltip
    d = rig.d
    hostile = "Last recording: C:/<b>x</b>&.csv"
    monkeypatch.setattr(d, "_record_block_reason", lambda: None)
    d._record_tip = hostile
    d._sync_controls()
    assert d.btn_record.toolTip() == plain_tooltip(hostile)
    assert d.shortcut_tooltip(d.btn_record) == plain_tooltip(f"{hostile} (R)")
    monkeypatch.setattr(d, "_record_block_reason", lambda: "<i>blocked</i>")
    d._sync_controls()
    assert d.btn_record.toolTip() == "<qt>&lt;i&gt;blocked&lt;/i&gt;</qt>"
    d._record_tip = ""
    d._set_record_tooltip(d._record_tip)
    assert d.btn_record.toolTip() == ""
