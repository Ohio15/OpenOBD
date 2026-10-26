"""Drive logging from the OBDX GT must not be stoppable by the app (2026-10-25).

Ron tried to log a drive and could not. Audit findings covered here:
  * Record was refused for the GT itself: the truck-mcp guard was a duck-typed
    hasattr(source, "channel_states"), which GtDataSource matched since v0.18.4.
  * The recording lived in %TEMP% until a save dialog at the end; cancel, a
    crash, or closing the app lost or stranded the drive.
  * App close never finalized the recording or released the GT port.
  * A USB unplug/replug (possibly onto a new COM number) was never recovered.
  * A silent GT took ~30 s per poll to report; an all-empty poll counted as
    healthy; a failed first poll left the COM port held.
  * Columns were fixed from the first poll; tft/run_time had no column.
"""
import csv
import os
import struct
import sys
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import gt as gtmod  # noqa: E402
from openobd import j2534  # noqa: E402
from openobd.gt import GtNoResponse, ObdxGt  # noqa: E402
from openobd.logbin import map_channel, parse_csv  # noqa: E402
from openobd.recorder import DriveRecorder, default_log_dirs  # noqa: E402
from openobd.transport import (  # noqa: E402
    DataSource, GtDataSource, LogReplaySource, Reading, Sample)
from openobd import tmsource  # noqa: E402

from test_module_map_truck import FakeTruckGt  # noqa: E402

LABELS = {"rpm": "Engine Speed (RPM)", "vss": "Vehicle Speed (mph)",
          "tft": "Trans Fluid Temp (F)"}


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


# --------------------------------------------------------------------------- #
# recorder: durable from the first row
# --------------------------------------------------------------------------- #
def test_recording_is_on_disk_in_its_final_place_before_it_is_closed(tmp_path):
    rec = DriveRecorder(["rpm", "vss"], LABELS, [str(tmp_path / "Logs")],
                        now=lambda: datetime(2026, 10, 25, 9, 30, 0))
    assert rec.path == str(tmp_path / "Logs" / "gt_log_20261025_093000.csv")
    assert _read(rec.path) == [["Time (s)", "Engine Speed (RPM)",
                                "Vehicle Speed (mph)"]]
    rec.write([Sample(1.0, {"rpm": 700.0}), Sample(1.5, {"rpm": 710.0, "vss": 3.0})])
    # Readable by another handle WITHOUT close(): a crash right now keeps it.
    assert _read(rec.path)[1:] == [["0.000", "700", ""], ["0.500", "710", "3"]]
    path, n = rec.close()
    assert (path, n) == (rec.path, 2) and os.path.exists(path)


def test_columns_hold_when_a_channel_appears_or_disappears(tmp_path):
    rec = DriveRecorder(["rpm", "tft"], LABELS, [str(tmp_path)])
    rec.write([Sample(0.0, {"rpm": 650.0}),               # tft not answering yet
               Sample(1.0, {"rpm": 900.0, "tft": 150.0}),  # comes alive
               Sample(2.0, {"tft": 151.0}),               # rpm drops out
               Sample(3.0, {"rpm": 905.0, "bogus": 1.0})])
    rec.close()
    rows = _read(rec.path)
    assert all(len(r) == 3 for r in rows)
    assert rows[1:] == [["0.000", "650", ""], ["1.000", "900", "150"],
                        ["2.000", "", "151"], ["3.000", "905", ""]]
    assert rec.ignored_keys == {"bogus"}


def test_two_recordings_in_the_same_second_never_overwrite(tmp_path):
    clock = lambda: datetime(2026, 10, 25, 9, 30, 0)  # noqa: E731
    a = DriveRecorder(["rpm"], LABELS, [str(tmp_path)], now=clock)
    b = DriveRecorder(["rpm"], LABELS, [str(tmp_path)], now=clock)
    assert a.path != b.path and b.path.endswith("_1.csv")
    a.close(), b.close()


def test_empty_recording_leaves_no_file(tmp_path):
    rec = DriveRecorder(["rpm"], LABELS, [str(tmp_path)])
    path, n = rec.close()
    assert n == 0 and not os.path.exists(path)


def test_falls_back_to_the_next_folder_when_the_first_is_unusable(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    rec = DriveRecorder(["rpm"], LABELS, [str(blocker / "Logs"), str(tmp_path / "ok")])
    assert os.path.dirname(rec.path) == str(tmp_path / "ok")
    rec.close()
    with pytest.raises(OSError, match="no writable folder"):
        DriveRecorder(["rpm"], LABELS, [str(blocker / "Logs")])


def test_a_write_failure_stops_cleanly_and_keeps_earlier_rows(tmp_path):
    rec = DriveRecorder(["rpm"], LABELS, [str(tmp_path)])
    rec.write([Sample(0.0, {"rpm": 700.0})])
    rec._fh.close()                       # the file goes away underneath us
    assert rec.write([Sample(1.0, {"rpm": 800.0})]) == 0
    assert rec.error and not rec.active
    path, n = rec.close()
    assert n == 1 and _read(path)[1:] == [["0.000", "700"]]


def test_default_log_dirs_prefers_documents_then_app_data():
    assert default_log_dirs("D:\\Docs", "C:\\AppData\\OpenOBD") == [
        os.path.join("D:\\Docs", "OpenOBD", "Logs"),
        os.path.join("C:\\AppData\\OpenOBD", "Logs")]
    assert default_log_dirs(None, "C:\\A") == [os.path.join("C:\\A", "Logs")]


def test_every_gt_channel_has_a_column_that_reloads_as_itself():
    from openobd.app import RECORD_LABELS, RECORD_ORDER
    assert set(gtmod.CANONICAL_KEYS) <= set(RECORD_ORDER)
    assert set(RECORD_ORDER) == set(RECORD_LABELS)
    for key, label in RECORD_LABELS.items():
        # run_time is not a canonical Log Analysis channel; it must at least
        # never be mistaken for the time axis.
        assert map_channel(label) == (None if key == "run_time" else key), label
    assert map_channel("Time (s)") == "time"


def test_a_recorded_file_reloads_in_log_analysis(tmp_path):
    rec = DriveRecorder(["rpm", "tft"], LABELS, [str(tmp_path)])
    rec.write([Sample(5.0, {"rpm": 700.0, "tft": 150.0}), Sample(5.5, {"rpm": 720.0})])
    rec.close()
    log = parse_csv(open(rec.path, encoding="utf-8").read())
    assert log.n_samples == 2
    assert log.series("rpm") == [700.0, 720.0]
    assert log.series("tft") == [150.0, None]


# --------------------------------------------------------------------------- #
# sources: which ones may be recorded
# --------------------------------------------------------------------------- #
def test_the_gt_is_recordable_and_truck_mcp_sessions_are_not():
    assert GtDataSource.is_store_record is False
    assert LogReplaySource.is_store_record is False
    assert tmsource.TmReplaySource.is_store_record is True
    assert tmsource.TmLiveSource.is_store_record is True
    # the old duck-typed signal no longer decides it
    assert hasattr(GtDataSource, "channel_states")


def test_gt_recordable_channels_cover_what_the_ecm_supports():
    src = GtDataSource()
    src._gt.supported = {"0C", "0D", "05"}
    src._keys = ["voltage"]            # only ATRV answered the first poll
    keys = set(src.recordable_channels())
    assert {"rpm", "vss", "ect", "voltage"} <= keys
    assert "maf" not in keys           # not supported by this ECM
    # no verified trans temp source: no column for a constant (v0.32.1)
    assert "tft" not in keys


def test_possible_keys_before_the_probe_is_the_whole_table():
    g = ObdxGt(port="X")
    assert set(g.possible_keys()) == set(gtmod.CANONICAL_KEYS)


# --------------------------------------------------------------------------- #
# GT link loss and recovery
# --------------------------------------------------------------------------- #
class ScriptedGt:
    """ObdxGt stand-in: poll_timed follows a script of {key: value} sweeps
    (or exceptions); open/close are counted."""

    def __init__(self, src, polls, opens=None, port="COM5"):
        self.src, self.polls, self.opens = src, list(polls), list(opens or [])
        self.port_name = port
        self.open_calls, self.close_calls, self.opened_on = 0, 0, []
        self.supported = set()

    def poll_timed(self):
        step = self.polls.pop(0)
        if not self.polls:
            self.src._stop.set()
        if isinstance(step, Exception):
            raise step
        return [Reading(k, v, float(i), float(i) + 0.05, i) for i, (k, v) in enumerate(step.items())]

    def open(self):
        self.open_calls += 1
        self.opened_on.append(self.port_name)
        step = self.opens.pop(0) if self.opens else "COM7"
        if isinstance(step, Exception):
            raise step
        self.port_name = step

    def close(self):
        self.close_calls += 1

    def possible_keys(self):
        return ["rpm", "voltage"]


def _scripted(polls, opens=None, port=None):
    src = GtDataSource(port=port, poll_interval=0)
    src.RECONNECT_BASE_S = 0.0
    src._gt = ScriptedGt(src, polls, opens, port=port or "COM5")
    src._t0 = 0.0
    return src


def test_a_dead_link_is_reopened_with_autodetect_and_samples_resume():
    gone = OSError("ClearCommError failed (device removed)")
    src = _scripted([{"rpm": 700.0}, gone, gone, gone, {"rpm": 710.0}],
                    opens=["COM7"])
    src._run()
    gt = src._gt
    assert gt.open_calls == 1
    assert gt.opened_on == [None]          # autodetect re-ran: new COM found
    assert src.port_name == "COM7" and src.reconnects == 1
    assert not src.failing() and src.status_message() is None
    assert [s.values["rpm"] for s in src.drain()] == [700.0, 710.0]


def test_a_pinned_port_is_reopened_as_pinned():
    gone = OSError("gone")
    src = _scripted([gone, gone, gone, {"rpm": 1.0}], opens=["COM3"], port="COM3")
    src._run()
    assert src._gt.opened_on == ["COM3"]


def test_failed_reopens_back_off_and_are_reported():
    gone = OSError("gone")
    src = _scripted([gone, gone, gone, {"rpm": 1.0}],
                    opens=[RuntimeError("No OBDX Pro GT found"), "COM9"])
    seen = []
    real = src._reconnect

    def spy():
        real()
        seen.append(src.status_message())
    src._reconnect = spy
    src._run()
    assert src._gt.open_calls == 2
    assert "reconnect attempt 1 failed: RuntimeError: No OBDX Pro GT found" in seen[0]
    assert src.reconnects == 1 and not src.failing()


def test_an_all_empty_poll_is_a_failure_not_a_live_source():
    src = _scripted([{"rpm": 700.0}, {}, {}])
    src._run()
    assert src.failing() and src.latest() is None
    assert "no data" in src.status_message()


def test_stop_interrupts_a_backoff_wait_and_releases_the_port():
    src = _scripted([OSError("x")] * 50, opens=[RuntimeError("absent")] * 50)
    src.RECONNECT_BASE_S = 30.0          # would block for a long time
    src._gt.polls.append({"rpm": 1.0})   # never reached
    t = threading.Thread(target=src._run)
    src._thread = t
    t.start()
    for _ in range(200):
        if src._reconnect_attempts >= 1:
            break
        threading.Event().wait(0.01)
    src.stop()
    assert not t.is_alive()
    assert src._gt.close_calls >= 1


def test_a_failed_first_poll_releases_the_port():
    src = GtDataSource()
    closed = []
    src._gt = SimpleNamespace(
        open=lambda: None, close=lambda: closed.append(1),
        poll_timed=lambda: (_ for _ in ()).throw(GtNoResponse("silent")),
        device="OBDX Pro GT", port_name="COM5")
    with pytest.raises(GtNoResponse):
        src.start()
    assert closed == [1] and src._thread is None


# --------------------------------------------------------------------------- #
# ObdxGt: fail fast on silence, explain a busy port, bounded writes
# --------------------------------------------------------------------------- #
class SilentAfterOpen(FakeTruckGt):
    silent = False

    def _answer(self, cmd):
        if self.silent and cmd.startswith("01"):
            return b""                     # no bytes, no '>' prompt
        return super()._answer(cmd)


def test_a_silent_gt_fails_the_poll_on_the_first_request(monkeypatch):
    monkeypatch.setattr(gtmod.time, "sleep", lambda s: None)
    fake = SilentAfterOpen()
    monkeypatch.setattr(gtmod, "serial", SimpleNamespace(Serial=lambda *a, **k: fake))
    g = ObdxGt(port="COM5")
    g.read_deadline_s = 0.02
    g.open()
    fake.silent = True
    before = len(fake.sent())
    with pytest.raises(GtNoResponse, match="COM5"):
        g.poll_once()
    assert len(fake.sent()) - before == 1  # did not walk the whole PID table


def test_open_passes_a_write_timeout(monkeypatch):
    monkeypatch.setattr(gtmod.time, "sleep", lambda s: None)
    seen = {}
    fake = FakeTruckGt()

    def ctor(*a, **k):
        seen.update(k)
        return fake
    monkeypatch.setattr(gtmod, "serial", SimpleNamespace(Serial=ctor))
    g = ObdxGt(port="COM5")
    g.read_deadline_s = 0.02
    g.open()
    assert seen.get("write_timeout") and seen.get("timeout")


def test_a_port_held_by_another_program_says_so(monkeypatch):
    def ctor(*a, **k):
        raise Exception("could not open port 'COM5': PermissionError(13, "
                        "'Access is denied.', None, 5)")
    monkeypatch.setattr(gtmod, "serial", SimpleNamespace(Serial=ctor))
    with pytest.raises(RuntimeError, match="in use by another program"):
        ObdxGt(port="COM5").open()


# --------------------------------------------------------------------------- #
# J2534 DLL bitness
# --------------------------------------------------------------------------- #
def _pe(tmp_path, name, machine):
    body = bytearray(0x80)
    body[:2] = b"MZ"
    struct.pack_into("<I", body, 0x3C, 0x40)
    body[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", body, 0x44, machine)
    p = tmp_path / name
    p.write_bytes(bytes(body))
    return str(p)


def test_pe_machine_reads_bitness(tmp_path):
    assert j2534.pe_machine(_pe(tmp_path, "a.dll", 0x8664)) == 0x8664
    assert j2534.pe_machine(_pe(tmp_path, "b.dll", 0x014C)) == 0x014C
    (tmp_path / "c.dll").write_bytes(b"not a pe")
    assert j2534.pe_machine(str(tmp_path / "c.dll")) is None
    assert j2534.pe_machine(str(tmp_path / "missing.dll")) is None


def test_find_dll_skips_a_dll_of_the_wrong_bitness(tmp_path, monkeypatch):
    wrong = 0x014C if j2534._PE_MACHINE_FOR_PROCESS == 0x8664 else 0x8664
    dll_wrong = _pe(tmp_path, "OBDXGT_wrong.dll", wrong)
    dll_right = _pe(tmp_path, "OBDXGT_right.dll", j2534._PE_MACHINE_FOR_PROCESS)
    views = {r"SOFTWARE\PassThruSupport.04.04": [("OBDX Pro - GT", dll_wrong)],
             r"SOFTWARE\WOW6432Node\PassThruSupport.04.04": [("OBDX Pro - GT", dll_right)]}

    class Key:
        def __init__(self, entries=None, lib=None):
            self.entries, self.lib = entries, lib

    def OpenKey(parent, sub):
        if isinstance(parent, Key):
            return Key(lib=dict(parent.entries)[sub])
        if sub not in views:
            raise OSError
        return Key(entries=views[sub])

    def EnumKey(k, i):
        if i >= len(k.entries):
            raise OSError
        return k.entries[i][0]

    fake = SimpleNamespace(HKEY_LOCAL_MACHINE=object(), OpenKey=OpenKey,
                           EnumKey=EnumKey,
                           QueryValueEx=lambda k, name: (k.lib, 1))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert j2534.find_dll() == dll_right


# --------------------------------------------------------------------------- #
# Dashboard: GT record, link loss mid-record, close mid-record
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class FakeLiveGt(GtDataSource):
    """A GtDataSource fed by the test instead of a poll thread."""

    def __init__(self):
        super().__init__(port="COM5")
        self._gt = SimpleNamespace(close=lambda: None, supported=set(),
                                   possible_keys=lambda: ["rpm", "tft", "voltage"],
                                   port_name="COM5")
        self._keys = ["voltage"]           # first poll only had ATRV
        self.stopped = False
        self._thread = object()            # live: only a live GT records

    def feed(self, t, **vals):
        s = Sample(t, vals)
        self._latest = s
        self._queue.append(s)

    def stop(self):
        self.stopped = True


@pytest.fixture
def dash(qapp, tmp_path):
    from openobd.app import Dashboard
    d = Dashboard()
    d._record_dirs = lambda: [str(tmp_path / "Logs")]
    src = FakeLiveGt()
    d.bind_source(src)
    yield d, src
    d.timer.stop()


def test_record_works_for_the_gt_and_survives_link_loss(dash):
    d, src = dash
    d.toggle_record()
    assert d.recording, d.status.text()
    path = d.last_record_path
    header = _read(path)[0]
    assert {"Engine Speed (RPM)", "Trans Fluid Temp (F)"} <= set(header)
    src.feed(1.0, rpm=700.0, voltage=14.1)
    d._tick()
    # link drops: two samples were queued before the failure was noticed
    src.feed(1.5, rpm=720.0, voltage=14.1)
    src.feed(2.0, rpm=730.0, voltage=14.0)
    src.fail_streak, src.last_error = 3, "OSError: gone"
    d._tick()                                  # latest() is None now
    assert d.recording
    assert "still recording" in d.status.text()
    assert len(_read(path)) == 4               # header + 3, none lost
    src.fail_streak = 0
    src.feed(9.0, rpm=800.0, tft=160.0, voltage=14.2)
    d._tick()
    d._finalize_record()
    rows = _read(path)
    assert [r[0] for r in rows[1:]] == ["0.000", "0.500", "1.000", "8.000"]
    assert "Saved 4 samples" in d.status.text()


def test_closing_the_app_mid_drive_finalizes_the_recording(dash):
    from openobd.app import MainWindow
    d, src = dash
    d.toggle_record()
    path = d.last_record_path
    src.feed(1.0, rpm=700.0)
    d._tick()
    src.feed(1.1, rpm=705.0)                   # produced after the last tick
    MainWindow._shutdown_pages(
        SimpleNamespace(dashboard=d, livedata=None, active_tests=None))
    assert not d.recording and src.stopped
    assert len(_read(path)) == 3


def test_rebinding_mid_record_finalizes_without_a_dialog(dash, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    d, src = dash
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: pytest.fail("dialog during teardown"))
    d.toggle_record()
    path = d.last_record_path
    src.feed(1.0, rpm=700.0)
    d._tick()
    d.bind_source(FakeLiveGt())
    assert not d.recording and len(_read(path)) == 2


def test_connect_while_connected_does_not_reopen_the_port(dash):
    d, src = dash
    src._thread = object()                     # looks connected
    d.connect_gt()
    assert not d._gt_connecting and "Already connected" in d.status.text()
    src.fail_streak = 3
    d.connect_gt()
    assert src._kick.is_set() and "Reconnecting" in d.status.text()
    src._thread = None


def test_truck_mcp_sessions_are_still_refused(dash, tmp_path):
    d, _src = dash

    class Store(DataSource):
        is_store_record = True

        def channels(self):
            return ["rpm"]
    d.bind_source(Store())
    d.toggle_record()
    assert not d.recording
