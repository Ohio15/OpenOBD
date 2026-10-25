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

REQUEST_S = 0.063            # old per-request time (sequential sweep era)

# Measured on this GT with truck-mcp (cassette no-accel-1000rpm 2026-09-18):
# round trip per Mode 01 request by PIDs in it. Linear between the points.
# Stated here independently of openobd.pollplan so the fake does not simply
# mirror the code under test.
MEASURED_COST = ((1, 0.0557), (3, 0.0550), (6, 0.0799))
ATRV_S = 0.0557              # plus the 50 ms settle wait poll_timed adds


def cost(n):
    pts = MEASURED_COST
    if n <= 1:
        return pts[0][1]
    for (n0, c0), (n1, c1) in zip(pts, pts[1:]):
        if n <= n1:
            return c0 + (c1 - c0) * (n - n0) / (n1 - n0)
    return pts[-1][1]


def _read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


# --------------------------------------------------------------------------- #
# A deterministic ELM327 + E38: a virtual clock that only moves while the
# "ECU" answers, so every exchange has exactly known send and reply times.
# Batched Mode 01 requests are answered like the real ELM with ATH0 ATS0
# ATCAF1: one single-frame line when the message fits 7 bytes, else the
# 3-hex-digit length line and 'N:' frame lines, last frame padded with 00.
# --------------------------------------------------------------------------- #
class Clock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now


def elm_reply(message: bytes) -> str:
    """ATH0/ATS0/ATCAF1 text for one ISO-TP message."""
    hexs = message.hex().upper()
    if len(message) <= 7:
        return hexs + "\r"
    lines = ["%03X" % len(message), "0:" + hexs[:12]]
    rest, idx = hexs[12:], 1
    while rest:
        chunk = rest[:14]
        lines.append(f"{idx & 0xF:X}:" + chunk.ljust(14, "0"))   # 00 padding
        rest, idx = rest[14:], idx + 1
    return "\r".join(lines) + "\r"


class TimedElm:
    """Answers 01<pids> after the measured cost for that many PIDs and logs
    (command, time written, time the reply became readable).

    values:         pid -> data bytes (default 0x40 repeated to its length)
    silent:         PIDs the ECM leaves out of every reply
    refuse_batches: a request with 2+ PIDs gets NO DATA (single PIDs answer)
    refuse_first:   only the first N batched requests get NO DATA (a
                    transient: lost frame / CAN ERROR)
    reverse:        answer a batch's PIDs in reverse order"""

    def __init__(self, clock, values=None, silent=(), refuse_batches=False,
                 refuse_first=0,
                 reverse=False):
        self.clock = clock
        self.values = values or {}
        self.silent = set(silent)
        self.refuse_batches = refuse_batches
        self.refuse_first = refuse_first
        self.reverse = reverse
        self.exchanges = []
        self.replies = []            # (command, reply text) per exchange
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

    def _data(self, pid):
        n = gtmod.PID_TABLE[pid].nbytes
        return bytes(self.values.get(pid, [0x40] * n))

    def write(self, data):
        cmd = data.decode().strip().upper()
        written = self.clock.now
        if cmd.startswith("01") and len(cmd) >= 4:
            pids = [cmd[i:i + 2] for i in range(2, len(cmd), 2)]
            self.clock.now += cost(len(pids))
            answer = [p for p in pids if p not in self.silent]
            if self.reverse:
                answer.reverse()
            transient = len(pids) > 1 and self.refuse_first > 0
            if transient:
                self.refuse_first -= 1
            if not answer or transient or (self.refuse_batches
                                           and len(pids) > 1):
                text = "NO DATA\r"
            else:
                msg = b"\x41" + b"".join(bytes([int(p, 16)]) + self._data(p)
                                          for p in answer)
                text = elm_reply(msg)
        elif cmd == "ATRV":
            self.clock.now += ATRV_S
            text = "12.6V\r"
        else:
            self.clock.now += ATRV_S
            text = "OK\r" if cmd.startswith("AT") else "NO DATA\r"
        self.exchanges.append((cmd, written, self.clock.now))
        self.replies.append((cmd, text))
        self._out = (text + "\r>").encode()


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


#: This truck's supported set: the 21 PIDs of the 2026-10-25 drive plus the
#: 13 added in v0.33.0 (all in that drive's supported-PID bitmap).
TRUCK_PIDS = tuple(gtmod.PID_TABLE)


# --------------------------------------------------------------------------- #
# the GT: per-request stamps, batched
# --------------------------------------------------------------------------- #
def test_each_value_is_stamped_inside_its_own_request_exchange(timed_gt):
    g, _clock = timed_gt(pids=("0C", "04", "49", "11"))
    readings = [r for r in g.poll_timed() if r.key != "voltage"]
    # One batched request for all four (tier order: rpm, tps, app, load).
    cmds = [e for e in g.ser.exchanges if e[0].startswith("01")]
    assert [c[0] for c in cmds] == ["010C114904"]
    _cmd, sent, ready = cmds[0]
    assert {r.key for r in readings} == {"rpm", "load", "app", "tps"}
    for r in readings:
        assert r.sent <= sent and ready <= r.received
        assert r.sent <= r.t <= r.received
        assert r.t == pytest.approx(sent + cost(4) / 2)
    # Values from one reply share its request number and exchange.
    assert len({(r.request, r.sent, r.received) for r in readings}) == 1


def test_requests_are_numbered_and_unanswered_ones_leave_a_gap(timed_gt):
    g, _clock = timed_gt(pids=("0C", "04", "49"), silent={"04"})
    first = g.poll_timed()
    # One batch (request 1); the ECM left 04 out, so load has no reading.
    # Cycle 0 is a SLOW cycle and PID 42 is unsupported: ATRV is request 2.
    assert [(r.key, r.request) for r in first] == [
        ("rpm", 1), ("app", 1), ("voltage", 2)]
    second = g.poll_timed()                     # FAST only: no ATRV
    assert [(r.key, r.request) for r in second] == [("rpm", 3), ("app", 3)]
    assert g.poll_once().keys() == {"rpm", "app", "voltage"}


def test_a_did_reply_is_dated_by_the_did_request_not_the_header_restore(timed_gt):
    g, _clock = timed_gt(pids=())
    g.request_did("1940", module="7E2")        # header switch, 22, restore
    cmd_22 = next(e for e in g.ser.exchanges if e[0] == "221940")
    assert g.last_exchange == (cmd_22[1], cmd_22[2])


def test_source_sample_time_is_the_cycle_midpoint(timed_gt):
    g, clock = timed_gt(pids=("04", "0C", "49", "11"))
    src = GtDataSource(poll_interval=0)
    src._gt = g
    src._t0 = clock.now
    s = src._sample(g.poll_timed())
    start, end = s.sweep_start(), s.sweep_end()
    assert start == pytest.approx(0.0)
    # one 4-PID batch, then ATRV with its 50 ms settle wait inside the
    # exchange (cycle 0 is a SLOW cycle; PID 42 unsupported).
    assert end == pytest.approx(cost(4) + ATRV_S + 0.05)
    assert s.t == pytest.approx(end / 2)
    rpm = next(r for r in s.readings if r.key == "rpm")
    assert rpm.t == pytest.approx(cost(4) / 2)
    assert all(start <= r.t <= end for r in s.readings)


def test_pids_answered_out_of_order_decode_to_the_right_channels(timed_gt):
    values = {"0C": [0x1A, 0xF8], "0D": [0x50], "11": [0x80], "49": [0x33],
              "0B": [0x21], "10": [0x01, 0x90]}
    g, _ = timed_gt(pids=tuple(values), values=values, reverse=True)
    got = {r.key: r.value for r in g.poll_timed()}
    assert got["rpm"] == pytest.approx(0x1AF8 / 4)
    assert got["vss"] == pytest.approx(0x50 * 0.6213712)
    assert got["map"] == 0x21
    assert got["maf"] == pytest.approx(0x190 / 100)
    assert got["tps"] == pytest.approx(0x80 * 100 / 255)
    assert got["app"] == pytest.approx(0x33 * 100 / 255)


def test_a_pid_absent_from_the_reply_gets_no_reading(timed_gt):
    pids = ("0C", "0D", "0B", "10", "11", "49")
    g, _ = timed_gt(pids=pids, silent={"10"})
    out = g.poll_timed()
    assert "maf" not in {r.key for r in out}
    assert {"rpm", "vss", "map", "tps", "app"} <= {r.key for r in out}
    assert sum(1 for e in g.ser.exchanges if e[0].startswith("01")) == 1
    assert not g.batching_refused       # a partial answer is not a refusal


def test_a_six_pid_batch_answered_in_one_single_frame(timed_gt):
    pids = ("0C", "0D", "0B", "10", "11", "49")
    g, _ = timed_gt(pids=pids, silent={"0B", "10", "11", "49"},
                    values={"0C": [0x0C, 0x80], "0D": [0x10]})
    out = g.poll_timed()
    batch = [t for c, t in g.ser.replies if c.startswith("01")]
    assert len(batch) == 1 and ":" not in batch[0]       # one single frame
    assert batch[0].startswith("410C0C800D10")
    assert {r.key: r.value for r in out if r.key != "voltage"} == {
        "rpm": pytest.approx(0x0C80 / 4), "vss": pytest.approx(0x10 * 0.6213712)}


def test_a_refused_batch_falls_back_to_one_pid_per_request(timed_gt):
    pids = ("0C", "0D", "0B", "10", "11", "49")
    g, _ = timed_gt(pids=pids, refuse_batches=True)
    first = g.poll_timed()
    cmds = [e[0] for e in g.ser.exchanges if e[0].startswith("01")]
    assert cmds[0] == "010C0D0B101149"                 # the batch: NO DATA
    assert cmds[1:] == ["01" + p for p in ("0C", "0D", "0B", "10", "11", "49")]
    # one refusing cycle is not enough: it could be a lost frame
    assert not g.batching_refused and g.batch_refusals == 1
    keys = [r.key for r in first if r.key != "voltage"]
    assert keys == ["rpm", "vss", "map", "maf", "tps", "app"]
    # the refused batch consumed request 1; the singles are 2..7
    assert [r.request for r in first if r.key != "voltage"] == list(range(2, 8))
    g.poll_timed()                                    # refused again: give up
    assert g.batching_refused and g.batch_refusals == 2
    g.ser.exchanges.clear()
    g.poll_timed()
    cmds = [e[0] for e in g.ser.exchanges if e[0].startswith("01")]
    assert all(len(c) == 4 for c in cmds) and len(cmds) == 6   # no re-batch
    assert g.poll_plan().batch_size == 1
    assert "refused batching" in g.poll_plan().describe()


def test_one_transient_batch_failure_does_not_end_batching(timed_gt):
    pids = ("0C", "0D", "0B", "10", "11", "49")
    g, _ = timed_gt(pids=pids, refuse_first=1)
    first = g.poll_timed()            # batch lost once; singles answer
    assert {r.key for r in first} >= {"rpm", "vss", "map", "maf", "tps", "app"}
    assert g.batch_refusals == 1 and not g.batching_refused
    g.ser.exchanges.clear()
    second = g.poll_timed()
    cmds = [e[0] for e in g.ser.exchanges if e[0].startswith("01")]
    assert cmds == ["010C0D0B101149"]                  # batching resumed
    assert {r.key for r in second} == {"rpm", "vss", "map", "maf", "tps", "app"}
    assert g._refusal_streak == 0 and g.poll_plan().batch_size == 6
    # a later single refusal starts a fresh count, still not enough
    g.ser.refuse_first = 1
    g.poll_timed()
    assert not g.batching_refused and g.batch_refusals == 2


def test_a_quiet_ecm_is_not_mistaken_for_a_batching_refusal(timed_gt):
    g, _ = timed_gt(pids=TRUCK_PIDS, silent=set(TRUCK_PIDS))
    out = g.poll_timed()
    cmds = [e[0] for e in g.ser.exchanges if e[0].startswith("01")]
    n_batches = len(g.poll_plan().batches_for_cycle(0))
    # first batch + its 6 singles, then each remaining batch once
    assert len(cmds) == n_batches + len(g.poll_plan().batches_for_cycle(0)[0])
    assert not g.batching_refused
    # ATRV still answers, so the transport sees a live GT, quiet ECM
    assert [r.key for r in out] == ["voltage"]


def test_atrv_fallback_is_numbered_and_on_the_slow_tier(timed_gt):
    g, _ = timed_gt(pids=("0C",))
    seen = []
    for _ in range(21):
        seen.append([(r.key, r.request) for r in g.poll_timed()])
    atrv = [i for i, s in enumerate(seen) if any(k == "voltage" for k, _ in s)]
    assert atrv == [0, 20]
    assert seen[0] == [("rpm", 1), ("voltage", 2)]
    assert seen[1] == [("rpm", 3)]


# --------------------------------------------------------------------------- #
# cadence with this truck's full supported set, at the measured costs
# --------------------------------------------------------------------------- #
def _drive(timed_gt, tmp_path, cycles):
    from openobd.app import RECORD_LABELS, RECORD_ORDER
    g, clock = timed_gt(pids=TRUCK_PIDS)
    src = GtDataSource(poll_interval=0)
    src._gt = g
    src._t0 = clock.now
    keys = [k for k in RECORD_ORDER if k in set(g.possible_keys())]
    rec = DriveRecorder(keys, RECORD_LABELS, [str(tmp_path)])
    samples = []
    for _ in range(cycles):
        s = src._sample(g.poll_timed())
        src._publish(s)
        samples.append(s)
    rec.write(src.drain())
    rec.close()
    return g, src, rec, samples


def test_fast_tier_cadence_with_the_full_truck_set(timed_gt, tmp_path):
    g, _src, rec, samples = _drive(timed_gt, tmp_path, 80)
    plan = g.poll_plan()
    assert len(plan.pids) == 34 and plan.batch_size == 6
    fast_only = [s for i, s in enumerate(samples) if i % 4]
    durations = [s.sweep_end() - s.sweep_start() for s in fast_only]
    assert max(durations) <= 0.25
    assert max(durations) == pytest.approx(plan.fast_cycle_s())  # 3 requests
    long = _read(rec.long_path)[1:]
    rpm_t = [float(r[0]) for r in long if r[1] == "rpm"]
    gaps = sorted(b - a for a, b in zip(rpm_t, rpm_t[1:]))
    median = gaps[len(gaps) // 2]
    assert median <= 0.25
    assert len(rpm_t) == 80                         # rpm read every cycle
    # mean cycle over the run matches the plan's prediction
    span = samples[-1].sweep_end() - samples[0].sweep_start()
    assert span / len(samples) == pytest.approx(plan.mean_cycle_s(), rel=0.05)


def test_not_due_channels_are_blank_in_rows_and_absent_from_sidecar(
        timed_gt, tmp_path):
    from openobd.app import RECORD_LABELS
    _g, _src, rec, _samples = _drive(timed_gt, tmp_path, 21)
    rows = _read(rec.path)
    header, data = rows[0], rows[1:]
    ect = header.index(RECORD_LABELS["ect"])          # MEDIUM
    baro = header.index(RECORD_LABELS["baro"])        # SLOW
    rpm = header.index(RECORD_LABELS["rpm"])          # FAST
    assert all(row[rpm] != "" for row in data)
    assert [i for i, row in enumerate(data) if row[ect] != ""] == [0, 4, 8, 12, 16, 20]
    assert [i for i, row in enumerate(data) if row[baro] != ""] == [0, 20]
    long = _read(rec.long_path)[1:]
    assert sum(1 for r in long if r[1] == "ect") == 6
    assert sum(1 for r in long if r[1] == "baro") == 2


def test_display_holds_slow_channels_without_writing_them_to_samples(
        timed_gt, tmp_path):
    _g, src, _rec, samples = _drive(timed_gt, tmp_path, 3)
    newest = samples[-1]
    assert "baro" not in newest.values                 # not due: not sampled
    assert "baro" in src.channel_values()              # but still displayed
    now = newest.sweep_end()
    states = src.channel_states(now=now)
    assert states["baro"] == "fresh" and states["rpm"] == "fresh"
    window = src.fresh_window_s("baro")
    assert window == pytest.approx(2 * 20 * (src._cycle_s), rel=1e-6)
    later = now + src.fresh_window_s("rpm") + 0.01
    assert src.channel_states(now=later)["rpm"] == "stale"
    assert src.channel_states(now=later)["baro"] == "fresh"
    src.fail_streak = src.FAIL_LIMIT
    assert set(src.channel_states(now=now).values()) == {"failed"}

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
    keys = ["rpm", "pedal", "load", "stft", "ltft"]
    rec = DriveRecorder(keys, RECORD_LABELS, [str(tmp_path)])
    sweeps = []
    req = 1
    for i in range(40):
        vals = {"rpm": 2000.0, "pedal": 20.0, "load": 45.0, "stft": 1.5, "ltft": 2.5}
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
