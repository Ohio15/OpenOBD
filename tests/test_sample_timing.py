"""Recorded times must say when each value was actually sampled (2026-10-25).

Brain 8d92fae5: GtDataSource stamped a whole 22-request poll sweep once, AFTER
it finished, and each request takes ~63 ms. A row's single time was therefore
the sweep END: load was 1.33 s older than its stamp, RPM 0.95 s, and pedal was
stamped newer than throttle. "Time (s)" also counted from GT connect, so the
first recorded row read 8.39 s.

Covered here:
  * every value is stamped inside its own request's send -> reply exchange;
  * the wide CSV keeps ONE row per sweep, its time is the sweep midpoint;
  * the long-format sidecar records each value's own request time, its
    request number, and the sweep's start and end;
  * time is zeroed at the first recorded sample;
  * the sidecar stays consistent with the CSV across a mid-record close and
    survives a crash (no close) like the CSV does;
  * the wide CSV still loads in Log Analysis with blank = missing and the
    joint features (regime report, table overlay) intact.
"""
import csv
import os
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import gt as gtmod  # noqa: E402
from openobd.calspec import Axis, Table  # noqa: E402
from openobd.gt import ObdxGt  # noqa: E402
from openobd.logbin import analyze_log, bin_log_to_table, parse_csv  # noqa: E402
from openobd.recorder import (  # noqa: E402
    SIDECAR_HEADER, DriveRecorder, sidecar_path)
from openobd.transport import GtDataSource, Reading, Sample  # noqa: E402

REQUEST_S = 0.063            # measured per-request time on the truck


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


# --------------------------------------------------------------------------- #
# A deterministic ELM327: a virtual clock that only moves while the "ECU"
# answers, so every exchange has exactly known send and reply times.
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now


class TimedElm:
    """Answers 01xx with a fixed byte per PID after REQUEST_S of latency and
    logs (command, time written, time the reply became readable)."""

    def __init__(self, clock, values=None, silent=()):
        self.clock = clock
        self.values = values or {}
        self.silent = set(silent)    # PIDs answered with NO DATA
        self.exchanges = []
        self._out = b""

    @property
    def in_waiting(self):
        return len(self._out)

    def read(self, n):
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk

    def reset_input_buffer(self):
        self._out = b""

    def close(self):
        pass

    def write(self, data):
        cmd = data.decode().strip().upper()
        written = self.clock.now
        self.clock.now += REQUEST_S
        self.exchanges.append((cmd, written, self.clock.now))
        if cmd.startswith("01") and cmd[2:] not in self.silent:
            b = self.values.get(cmd[2:], 0x40)
            self._out = f"41{cmd[2:]}{b:02X}{b:02X}\r\r>".encode()
        elif cmd == "ATRV":
            self._out = b"12.6V\r\r>"
        elif cmd.startswith("AT"):
            self._out = b"OK\r\r>"
        else:
            self._out = b"NO DATA\r\r>"


@pytest.fixture
def timed_gt(monkeypatch):
    clock = Clock()
    # Mode 22 DIDs are another change's business (the table is being
    # emptied); pin it so request numbering here depends on Mode 01 only.
    monkeypatch.setattr(gtmod, "DID_TABLE", {})
    monkeypatch.setattr(gtmod.time, "monotonic", clock)
    monkeypatch.setattr(gtmod.time, "sleep",
                        lambda s: setattr(clock, "now", clock.now + s))

    def build(pids=("0C", "04", "49", "11"), **kw):
        g = ObdxGt(port="FAKE")
        g.ser = TimedElm(clock, **kw)
        g._header = "7E0"
        g.supported = set(pids)
        return g, clock
    return build


# --------------------------------------------------------------------------- #
# the GT: per-request stamps
# --------------------------------------------------------------------------- #
def test_each_value_is_stamped_inside_its_own_request_exchange(timed_gt):
    g, _clock = timed_gt(pids=("0C", "04", "49", "11"))
    readings = g.poll_timed()
    exchanges = {cmd: (sent, ready) for cmd, sent, ready in g.ser.exchanges}
    by_key = {r.key: r for r in readings}
    for key, pid in (("rpm", "0C"), ("load", "04"), ("app", "49"), ("tps", "11")):
        r = by_key[key]
        sent, ready = exchanges["01" + pid]
        assert r.sent <= sent and ready <= r.received
        assert r.sent <= r.t <= r.received
        assert r.t == pytest.approx(sent + REQUEST_S / 2)
    # Stamps follow request order: each value is 63 ms after the one before,
    # instead of all sharing the sweep's end.
    ts = [r.t for r in readings]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)
    assert ts[1] - ts[0] == pytest.approx(REQUEST_S)


def test_requests_are_numbered_and_unanswered_ones_leave_a_gap(timed_gt):
    g, _clock = timed_gt(pids=("0C", "04", "49"), silent={"04"})
    first = g.poll_timed()
    # PID_TABLE order is 04, 0C, 49, then ATRV. 04 answered NO DATA: its
    # request number (1) is consumed, so the gap is visible in the sidecar.
    assert [(r.key, r.request) for r in first] == [
        ("rpm", 2), ("app", 3), ("voltage", 4)]
    second = g.poll_timed()
    assert [r.request for r in second] == [6, 7, 8]
    assert g.poll_once().keys() == {"rpm", "app", "voltage"}


def test_a_did_reply_is_dated_by_the_did_request_not_the_header_restore(timed_gt):
    g, _clock = timed_gt(pids=())
    g.request_did("1940", module="7E2")        # header switch, 22, restore
    cmd_22 = next(e for e in g.ser.exchanges if e[0] == "221940")
    assert g.last_exchange == (cmd_22[1], cmd_22[2])


def test_source_sample_time_is_the_sweep_midpoint(timed_gt):
    g, clock = timed_gt(pids=("04", "0C", "49", "11"))
    src = GtDataSource(poll_interval=0)
    src._gt = g
    src._t0 = clock.now
    s = src._sample(g.poll_timed())
    start, end = s.sweep_start(), s.sweep_end()
    assert start == pytest.approx(0.0)
    # 4 PIDs + ATRV = 5 requests, ATRV with its 50 ms settle wait inside
    # the exchange. The old stamp was this end of the sweep.
    assert end == pytest.approx(5 * REQUEST_S + 0.05)
    assert s.t == pytest.approx(end / 2)
    rpm = next(r for r in s.readings if r.key == "rpm")
    assert rpm.t == pytest.approx(1.5 * REQUEST_S)     # 2nd request's midpoint
    assert all(start <= r.t <= end for r in s.readings)
    assert abs(s.t - rpm.t) <= (end - start) / 2


# --------------------------------------------------------------------------- #
# recorder: wide row per sweep at its midpoint, sidecar with request times
# --------------------------------------------------------------------------- #
# Same headers as app.RECORD_LABELS (checked below), so recordings made by
# the Dashboard cross-check against them too.
LABELS = {"rpm": "Engine Speed (RPM)", "load": "Engine Load (%)",
          "app": "Accelerator Pedal (%)", "tps": "Throttle Position (%)",
          "stft": "Short Term Fuel Trim (%)", "ltft": "Long Term Fuel Trim (%)"}


def sweep(t_start, values, first_request):
    """A GT-shaped Sample: one request per value, REQUEST_S each."""
    readings = tuple(
        Reading(k, float(v), t_start + i * REQUEST_S,
                t_start + (i + 1) * REQUEST_S, first_request + i)
        for i, (k, v) in enumerate(values.items()))
    s = Sample(0.0, {r.key: r.value for r in readings}, readings)
    s.t = (s.sweep_start() + s.sweep_end()) / 2
    return s


def test_wide_row_is_the_sweep_midpoint_zeroed_at_the_first_sample(tmp_path):
    rec = DriveRecorder(["rpm", "load"], LABELS, [str(tmp_path)])
    # The source clock counts from GT connect: recording starts 8.39 s in.
    a = sweep(8.39, {"load": 40.0, "rpm": 2000.0}, 101)
    b = sweep(9.85, {"load": 41.0, "rpm": 2010.0}, 103)
    rec.write([a, b])
    rec.close()
    rows = _read(rec.path)
    assert rows[0] == ["Time (s)", "Engine Speed (RPM)", "Engine Load (%)"]
    assert rows[1] == ["0.000", "2000", "40"]          # one row per sweep
    assert rows[2] == [f"{b.t - a.t:.3f}", "2010", "41"]
    long = _read(rec.long_path)
    assert long[0] == SIDECAR_HEADER
    half = REQUEST_S                                    # sweep of 2 requests
    first = long[1:3]
    assert [r[1] for r in first] == ["load", "rpm"]
    assert [int(r[3]) for r in first] == [101, 102]
    # each value at its own request midpoint, relative to the same zero
    assert float(first[0][0]) == pytest.approx(-half / 2)
    assert float(first[1][0]) == pytest.approx(half / 2)
    for line in long[1:]:
        t, start, end = float(line[0]), float(line[4]), float(line[5])
        assert start <= t <= end and end - start == pytest.approx(2 * REQUEST_S)
    assert float(long[1][4]) == pytest.approx(-REQUEST_S)
    assert float(long[1][5]) == pytest.approx(REQUEST_S)


def test_test_labels_are_the_dashboard_labels():
    from openobd.app import RECORD_LABELS
    assert all(RECORD_LABELS[k] == v for k, v in LABELS.items())


def test_sidecar_values_are_lossless(tmp_path):
    rec = DriveRecorder(["rpm"], LABELS, [str(tmp_path)])
    rec.write([sweep(0.0, {"rpm": 1234.5678901, "bogus": 0.1}, 1)])
    rec.close()
    assert _read(rec.path)[1] == ["0.000", "1234.57"]   # wide view: 6 sig
    long = _read(rec.long_path)[1:]
    assert float(long[0][2]) == 1234.5678901
    assert long[1][1:3] == ["bogus", "0.1"]             # no column, still kept


def test_a_sample_without_readings_still_records_and_zeroes(tmp_path):
    rec = DriveRecorder(["rpm"], LABELS, [str(tmp_path)])
    rec.write([Sample(5.0, {"rpm": 700.0}), Sample(5.5, {"rpm": 710.0})])
    rec.close()
    assert [r[0] for r in _read(rec.path)[1:]] == ["0.000", "0.500"]
    assert _read(rec.long_path)[1:] == [["0.000000", "rpm", "700.0", "", "", ""],
                                        ["0.500000", "rpm", "710.0", "", "", ""]]


def test_sidecar_is_named_after_the_csv_and_never_reused(tmp_path):
    from datetime import datetime
    clock = lambda: datetime(2026, 10, 25, 9, 30, 0)  # noqa: E731
    stray = tmp_path / "gt_log_20261025_093000.long.csv"
    stray.write_text("someone else's\n")
    rec = DriveRecorder(["rpm"], LABELS, [str(tmp_path)], now=clock)
    assert rec.path.endswith("gt_log_20261025_093000_1.csv")
    assert rec.long_path == sidecar_path(rec.path)
    assert stray.read_text() == "someone else's\n"
    assert not os.path.exists(tmp_path / "gt_log_20261025_093000.csv")
    path, n = rec.close()
    assert n == 0 and not os.path.exists(path)
    assert not os.path.exists(rec.long_path)            # empty: both removed


# --------------------------------------------------------------------------- #
# integrity: crash and close mid-record
# --------------------------------------------------------------------------- #
def _cross_check(csv_path, long_path, sweeps):
    """Every wide cell has its sidecar line in the same sweep, and the
    sidecar holds every reading fed, once."""
    rows = _read(csv_path)
    header, data = rows[0], rows[1:]
    long = _read(long_path)[1:]
    assert len(data) == len(sweeps)
    assert len(long) == sum(len(s.readings) for s in sweeps)
    assert len({line[3] for line in long}) == len(long)  # request ids unique
    by_req = {int(line[3]): line for line in long}
    for row, s in zip(data, sweeps):
        row_t = float(row[0])
        for r in s.readings:
            line = by_req[r.request]
            assert float(line[2]) == r.value
            start, end = float(line[4]), float(line[5])
            assert start <= row_t <= end                 # row inside its sweep
            col = header.index(LABELS[r.key])
            assert float(row[col]) == pytest.approx(r.value, rel=1e-5)


def test_a_crash_mid_record_leaves_both_files_consistent(tmp_path):
    rec = DriveRecorder(["rpm", "load"], LABELS, [str(tmp_path)])
    sweeps = [sweep(1.0 + 1.4 * i, {"load": 40.0 + i, "rpm": 2000.0 + i},
                    1 + 2 * i) for i in range(5)]
    rec.write(sweeps[:3])
    rec.write(sweeps[3:])
    # No close(): read through other handles, as after a crash.
    for p in (rec.path, rec.long_path):
        with open(p, "rb") as fh:
            assert fh.read().endswith(b"\n")             # no torn last line
    _cross_check(rec.path, rec.long_path, sweeps)
    rec.close()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_closing_the_app_mid_record_finalizes_csv_and_sidecar(qapp, tmp_path):
    from openobd.app import Dashboard, MainWindow
    d = Dashboard()
    d._record_dirs = lambda: [str(tmp_path / "Logs")]
    src = GtDataSource(port="COM5")
    src._gt = SimpleNamespace(close=lambda: None, supported=set(),
                              possible_keys=lambda: ["rpm", "load", "voltage"],
                              port_name="COM5")
    src.stop = lambda: None
    d.bind_source(src)
    try:
        d.toggle_record()
        assert d.recording, d.status.text()
        rec = d._recorder
        sweeps = [sweep(8.39 + 1.4 * i, {"load": 40.0 + i, "rpm": 2000.0 + i},
                        1 + 2 * i) for i in range(4)]
        for s in sweeps[:3]:
            src._latest = s
            src._queue.append(s)
            d._tick()
        src._queue.append(sweeps[3])                     # after the last tick
        MainWindow._shutdown_pages(
            SimpleNamespace(dashboard=d, livedata=None, active_tests=None))
        assert not d.recording and rec._fh is None and rec._long_fh is None
        _cross_check(rec.path, rec.long_path, sweeps)
        assert _read(rec.path)[1][0] == "0.000"
    finally:
        d.timer.stop()


# --------------------------------------------------------------------------- #
# Log Analysis: the wide CSV still loads and its joint features still work
# --------------------------------------------------------------------------- #
def test_wide_csv_loads_with_blanks_missing_and_joint_features_intact(tmp_path):
    from openobd.app import RECORD_LABELS
    keys = ["rpm", "app", "load", "stft", "ltft"]
    rec = DriveRecorder(keys, RECORD_LABELS, [str(tmp_path)])
    sweeps = []
    req = 1
    for i in range(40):
        vals = {"rpm": 2000.0, "app": 20.0, "load": 45.0, "stft": 1.5, "ltft": 2.5}
        if i == 7:
            del vals["ltft"]                             # PID silent this sweep
        sweeps.append(sweep(8.0 + 1.4 * i, vals, req))
        req += len(vals)
    rec.write(sweeps)
    rec.close()
    log = parse_csv(open(rec.path, encoding="utf-8").read())
    assert log.n_samples == 40                           # one row per sweep
    assert log.series("ltft")[7] is None                 # blank = missing
    assert log.series("time")[0] == 0.0
    rep = analyze_log(log)
    cruise = {r.name: r for r in rep.regimes}["cruise"]
    assert cruise.n == 40
    assert cruise.stft_mean == pytest.approx(1.5)
    assert cruise.ltft_mean == pytest.approx(2.5)
    table = Table(name="t", x_axis=Axis("RPM", "RPM", [800, 1600, 2400]),
                  y_axis=Axis("Load", "%", [20, 40, 60]),
                  values=[[0.0] * 3 for _ in range(3)])
    ov = bin_log_to_table(log, table, "rpm", "load")
    assert ov.total_binned == 40 and ov.count_grid()[1][1] == 40
