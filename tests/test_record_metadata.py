"""
v0.33.0 wave 2: the per-recording metadata sidecar (<stem>.meta.json), the
normalized pedal (openobd.pedal) and the ethanol-estimate labelling.

Evidence these tests hold the code to: the 2026-10-25 drive
(tests/data/gt_log_20261025_141431.csv, 1463 rows) read APP D 19 % at rest and
61 % flat out, so a raw "pedal >= 70" WOT test found nothing; PID 52 ethanol is
the ECM's estimate. Mode 09 reply shapes are the ones truck-mcp captured from
this truck's ECM (captures/first-connect-2026-08-03.json).
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import gt as gtmod  # noqa: E402
from openobd import pedal as pedalmod  # noqa: E402
from openobd.logbin import (  # noqa: E402
    BASIS_APP_CAL, BASIS_LOAD_MAP, PE_REGIME, analyze_log, attach_long,
    calibrate_pedal_from_log, detect_shift_points, map_channel, parse_csv)
from openobd.pedal import PedalCal, PedalLearner  # noqa: E402
from openobd.recorder import (  # noqa: E402
    DriveRecorder, meta_path, read_meta, sidecar_path)
from openobd.transport import GtDataSource, Reading, Sample  # noqa: E402

from test_sample_timing import TRUCK_PIDS, timed_gt  # noqa: E402,F401

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVE = os.path.join(HERE, "data", "gt_log_20261025_141431.csv")
LABELS = {"rpm": "Engine Speed (RPM)", "app": "Accelerator Pedal (%)",
          "pedal": "Pedal, normalized (%)"}


def _drive_log():
    with open(DRIVE, encoding="utf-8") as fh:
        return parse_csv(fh.read(), source=os.path.basename(DRIVE))


# --------------------------------------------------------------------------- #
# Mode 09: realistic replies (this truck's ECM)
# --------------------------------------------------------------------------- #
VIN = "3GCRKTE35AG150432"
CALIDS = ["12636005", "12637641", "12650874", "12625892", "12620683",
          "12650881", "12620820", "12650898"]
CVNS = [bytes.fromhex(h) for h in ("1A2B3C4D", "00FF10E0")]


def _calid_msg():
    body = b"".join(c.encode().ljust(16, b"\x00") for c in CALIDS)
    return b"\x49\x04" + bytes([len(CALIDS)]) + body


INFO = {"02": b"\x49\x02\x01" + VIN.encode(),
        "04": _calid_msg(),
        "06": b"\x49\x06\x02" + b"".join(CVNS)}

#: The real headers-on functional captures (7E8 ECM, 7EA TCM and 7EB
#: interleaved), truck-mcp first-connect 2026-08-03.
REAL_VIN_ATH1 = "7E81014490201334743 7E821524B5445333541 7E82247313530343332"
REAL_CALID_ATH1 = (
    "7E81083490408313236 7EB1033490403323038 7EA1043490404323432 "
    "7E82133363030350000 7EB2132373735330000 7EA2135343930390000 "
    "7E82200000000000031 7EB2200000000000032 7EA2200000000000032 "
    "7E82332363337363431 7EB2335383632323636 7EA2334323536303434 "
    "7E82400000000000000 7EB2400000000000000 7EA2400000000000000 "
    "7E82500313236353038 7EB2500323038323737 7EA2500323432353633 "
    "7E82637340000000000 7EB2634390000000000 7EA2635320000000000 "
    "7E82700000031323632 7EB27000000AAAAAAAA 7EA2700000032343235 "
    "7E82835383932000000 7EA2835373231000000 7E82900000000003132 "
    "7EA290000000000AAAA 7E82A36323036383300 7E82B00000000000000 "
    "7E82C31323635303838 7E82D31000000000000 7E82E00003132363230 "
    "7E82F38323000000000 7E82000000000313236 7E82135303839380000 "
    "7E822000000000000AA")


def test_vin_calid_cvn_decode_from_multiframe_replies(timed_gt):
    g, _clock = timed_gt(pids=TRUCK_PIDS, info=INFO)
    ident = g.read_vehicle_identity()
    assert ident["examined"] and ident["header"] == "7E0"
    assert ident["vin"]["value"] == VIN and ident["vin"]["examined"]
    assert ident["vin"]["raw"].startswith("490201")
    assert [i["text"] for i in ident["calids"]["items"]] == CALIDS
    assert ident["calids"]["count"] == 8
    assert all(len(i["hex"]) == 32 for i in ident["calids"]["items"])
    assert [i["hex"] for i in ident["cvns"]["items"]] == ["1A2B3C4D", "00FF10E0"]
    # the 131-byte CALID message really went multi-frame, with index wrap
    calid_text = dict(g.ser.replies)["0904"]
    assert calid_text.startswith("083\r0:490408") and "\r0:" in calid_text[5:]


def test_identity_decodes_the_real_headers_on_captures():
    vin = gtmod.parse_mode09_reply(REAL_VIN_ATH1, "02")
    cal = gtmod.parse_mode09_reply(REAL_CALID_ATH1, "04")
    ident = gtmod.decode_vehicle_identity({"vin": vin, "calids": cal})
    assert ident["vin"]["value"] == VIN
    assert [i["text"] for i in ident["calids"]["items"]] == CALIDS  # 7E8's only


def test_unanswered_identity_is_not_examined_never_an_empty_value(timed_gt):
    g, _clock = timed_gt(pids=TRUCK_PIDS)                 # 09xx -> NO DATA
    ident = g.read_vehicle_identity()
    assert ident["examined"] is False
    for name in ("vin", "calids", "cvns"):
        entry = ident[name]
        assert entry["examined"] is False and "NO DATA" in entry["why"]
        assert "value" not in entry and "items" not in entry


@pytest.mark.parametrize("msg, why", [
    (b"\x49\x02\x01" + b"3GCRKTE35AG15", "does not match"),     # short
    (b"\x7F\x09\x12", "NRC 0x12"),
    (b"\x49\x02\x01" + b"3GCRKTE35AG15043\x01", "not 17 printable"),
])
def test_bad_mode09_replies_say_why(timed_gt, msg, why):
    g, _clock = timed_gt(pids=TRUCK_PIDS, info={"02": msg})
    vin = g.read_vehicle_identity()["vin"]
    assert vin["examined"] is False and why in vin["why"]


VIN_HEX = "490201" + VIN.encode().hex().upper()


def test_response_pending_is_skipped_and_the_final_reply_used():
    # headers off: '7F0978' then the multi-frame VIN in the same read
    text = ("7F0978 014 0:" + VIN_HEX[:12] + " 1:" + VIN_HEX[12:26]
            + " 2:" + VIN_HEX[26:])
    p = gtmod.parse_mode09_reply(text, "02")
    assert p["examined"] and bytes(p["items"][0]).decode() == VIN
    # headers on: the ECM's pending single frame, then its real message
    on = "7E8037F0978 " + REAL_VIN_ATH1
    assert gtmod.parse_mode09_reply(on, "02")["examined"]


def test_response_pending_alone_is_not_examined_and_other_nrcs_refuse():
    p = gtmod.parse_mode09_reply("7F0978", "02")
    assert p == {"examined": False,
                 "why": "not examined (response pending, no final reply)"}
    r = gtmod.parse_mode09_reply("7F0931", "02")
    assert r["examined"] is False and "refused" in r["why"] \
        and "NRC 0x31" in r["why"]


def test_read_vin_is_one_request_and_identity_reuses_it(timed_gt):
    g, _clock = timed_gt(pids=TRUCK_PIDS, info=INFO)
    vin = g.read_vin()
    assert vin["examined"] and vin["value"] == VIN
    ident = g.read_vehicle_identity(vin=vin)
    sent = [c for c, *_ in g.ser.exchanges]
    assert sent.count("0902") == 1 and "0904" in sent and "0906" in sent
    assert ident["vin"] is vin and ident["calids"]["count"] == 8


def test_recording_context_holds_poll_and_restores_the_poll_state(timed_gt):
    g, clock = timed_gt(pids=TRUCK_PIDS, info=INFO)
    src = GtDataSource(poll_interval=0)
    src._gt = g
    src._t0 = clock.now
    src._publish(src._sample(g.poll_timed()))
    ctx = src.read_recording_context()
    assert ctx["identity"]["vin"]["value"] == VIN
    # the fake answers 03/07/0A with NO DATA: not examined, never "no codes"
    d = ctx["dtcs_at_start"]
    assert d["examined"] is False and d["stored"] is None
    # read_dtcs went functional with headers on, and _restore_default put
    # the poll state back: physical 7E0, headers off
    sent = [c for c, *_ in g.ser.exchanges]
    assert "ATSH7DF" in sent and "ATH1" in sent
    assert g.ser.header == "7E0" and g.ser.headers_on is False
    assert g._header == "7E0"
    n = len(g.ser.exchanges)
    rs = g.poll_timed()
    after = [c for c, *_ in g.ser.exchanges[n:]]
    assert rs and after[0].startswith("01") and "ATSH7E0" not in after
    assert {"rpm", "app", "load"} <= {r.key for r in rs}
    # identity is cached for this connection once the VIN read
    ctx2 = src.read_recording_context()
    assert ctx2["identity"] is ctx["identity"]
    assert sum(1 for c, *_ in g.ser.exchanges if c == "0902") == 1
    masks = ctx["supported"]["masks"]
    assert set(masks) == {"0100", "0120", "0140", "0160"}


def test_supported_report_masks_and_reasons():
    g = gtmod.ObdxGt(port="X")
    g.supported_masks = {"0100": 0xBE3FB813, "0120": 0x8007A010}
    g.supported = {"01", "0C"}
    g._probe_stop = ("0120", "its next-range bit is clear")
    rep = g.supported_report()
    assert rep["masks"]["0100"] == "BE3FB813"
    assert rep["masks"]["0140"] == {
        "examined": False, "why": "not asked: 0120 its next-range bit is clear"}
    assert rep["pids"] == ["01", "0C"]


# --------------------------------------------------------------------------- #
# the metadata sidecar
# --------------------------------------------------------------------------- #
def _rec(tmp_path, **kw):
    return DriveRecorder(["rpm", "app", "pedal"], LABELS, [str(tmp_path)], **kw)


def _sample(t, req, **vals):
    rs = tuple(Reading(k, v, t, t + 0.05, req + i)
               for i, (k, v) in enumerate(vals.items()))
    return Sample(t + 0.025, dict(vals), rs)


def test_meta_is_written_at_start_with_not_examined_vehicle(tmp_path):
    why = {"examined": False, "why": "the fake answered nothing"}
    rec = _rec(tmp_path, meta={"vehicle": why, "dtcs_at_start": why})
    assert rec.meta_path == meta_path(rec.path)
    on_disk = read_meta(rec.path)               # complete before any sample
    assert on_disk["schema"] == 1 and on_disk["vehicle"] == why
    assert on_disk["files"] == {"csv": os.path.basename(rec.path),
                                "long": os.path.basename(rec.long_path),
                                "meta": os.path.basename(rec.meta_path)}
    assert on_disk["utc_start"].endswith("+00:00")
    assert on_disk["markers"] == [] and "utc_end" not in on_disk
    from openobd import __version__
    assert on_disk["openobd_version"] == __version__
    rec.write([_sample(10.0, 1, rpm=800.0)])
    rec.close()


def test_markers_are_persisted_and_reread_and_the_csv_is_untouched(tmp_path):
    rec = _rec(tmp_path)
    assert rec.add_marker(5.0) is None                   # no sample yet
    rec.write([_sample(10.0, 1, rpm=800.0)])
    with open(rec.path, "rb") as fh:
        before = fh.read()
    m1 = rec.add_marker(12.5)
    m2 = rec.add_marker()                                # no clock: last sample
    assert m1 == {"t": 2.475, "label": "mark 1"}    # zero = 10.025 midpoint
    assert m2 == {"t": 0.0, "label": "mark 2"}
    assert read_meta(rec.path)["markers"] == [m1, m2]
    with open(rec.path, "rb") as fh:
        assert fh.read() == before
    rec.close()
    assert read_meta(rec.path)["markers"] == [m1, m2]


def test_meta_rewrite_is_atomic(tmp_path, monkeypatch):
    rec = _rec(tmp_path)
    rec.write([_sample(10.0, 1, rpm=800.0)])
    rec.add_marker(11.0)
    good = read_meta(rec.path)
    calls = []
    real_replace = os.replace

    def replace(src, dst):
        calls.append((src, dst))
        raise OSError("power cut before the rename")
    monkeypatch.setattr(os, "replace", replace)
    assert rec.add_marker(12.0) is not None              # recorded in memory
    # temp + replace, never an in-place write: the old document survives
    assert calls == [(rec.meta_path + ".tmp", rec.meta_path)]
    assert read_meta(rec.path) == good
    assert not os.path.exists(rec.meta_path + ".tmp")
    assert "power cut" in rec.meta_error
    monkeypatch.setattr(os, "replace", real_replace)
    rec.close()                                          # recovers on retry
    assert len(read_meta(rec.path)["markers"]) == 2


def test_a_torn_temp_file_never_damages_the_metadata(tmp_path, monkeypatch):
    rec = _rec(tmp_path)
    rec.write([_sample(10.0, 1, rpm=800.0)])
    rec.add_marker(11.0)
    good = read_meta(rec.path)
    # a crash mid temp-write: a torn .tmp left behind, the original intact
    tmp = rec.meta_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write('{"schema": 1, "markers": [{"t": 1')
    assert read_meta(rec.path) == good
    # a failure while the temp is being written: original intact, temp gone
    real_fsync = os.fsync
    calls = {"n": 0}

    def fsync(fd):
        calls["n"] += 1
        raise OSError("disk yanked mid-write")
    monkeypatch.setattr(os, "fsync", fsync)
    rec.add_marker(12.0)
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert calls["n"] == 1 and rec.meta_error
    assert read_meta(rec.path) == good and not os.path.exists(tmp)
    # the next rewrite succeeds, replaces the torn state and clears the error
    rec.add_marker(13.0)
    assert rec.meta_error is None
    assert [m["label"] for m in read_meta(rec.path)["markers"]] == [
        "mark 1", "mark 2", "mark 3"]
    rec.close()


def test_a_malformed_sample_stops_the_recorder_without_raising(tmp_path):
    rec = _rec(tmp_path)
    rec.write([_sample(10.0, 1, rpm=800.0)])
    bad = Sample(11.0, {"rpm": None},
                 (Reading("rpm", None, 11.0, 11.05, 2),))
    assert rec.write([bad]) == 0                         # no exception
    assert rec.error and "TypeError" in rec.error and not rec.active
    rec.close()
    assert read_meta(rec.path)["error"] == rec.error


def test_empty_recording_removes_all_three_files(tmp_path):
    rec = _rec(tmp_path)
    paths = (rec.path, rec.long_path, rec.meta_path)
    assert all(os.path.exists(p) for p in paths)
    rec.close()
    assert not any(os.path.exists(p) for p in paths)
    assert os.listdir(tmp_path) == []


def test_close_adds_end_rows_and_error(tmp_path):
    rec = _rec(tmp_path)
    rec.write([_sample(10.0, 1, rpm=800.0), _sample(11.0, 2, rpm=900.0)])
    rec.close()
    meta = read_meta(rec.path)
    assert meta["rows"] == 2 and meta["error"] is None
    assert meta["duration_s"] == 1.0
    assert meta["utc_end"] >= meta["utc_start"]
    rec.close()                                          # idempotent
    assert read_meta(rec.path)["rows"] == 2


def test_a_stray_meta_file_moves_the_recording_to_a_fresh_stem(tmp_path):
    from datetime import datetime
    stamp = datetime(2026, 10, 25, 14, 14, 31)
    (tmp_path / "gt_log_20261025_141431.meta.json").write_text("{}")
    rec = _rec(tmp_path, now=lambda: stamp)
    assert rec.path.endswith("gt_log_20261025_141431_1.csv")
    assert (tmp_path / "gt_log_20261025_141431.meta.json").read_text() == "{}"
    rec.close()


# --------------------------------------------------------------------------- #
# pedal normalization: learner, derived channel, sidecars
# --------------------------------------------------------------------------- #
def test_the_drive_log_calibrates_to_its_own_rest_and_full():
    cal = calibrate_pedal_from_log(_drive_log())
    assert cal.rest == pytest.approx(19.2157, abs=0.01)
    assert cal.full == pytest.approx(61.1765, abs=0.01)
    assert cal.ready and "gt_log_20261025_141431.csv" in cal.source


def _drive_rows():
    with open(DRIVE, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    def f(r, k):
        return float(r[k]) if r[k] else None
    return [(f(r, "Engine Speed (RPM)"), f(r, "Accelerator Pedal (%)"),
             f(r, "Engine Load (%)"), f(r, "Manifold Absolute Pressure (kPa)"))
            for r in rows]


def test_drive_log_wot_counts_cross_checked_independently():
    """The 50 / 14 counts recomputed here from the CSV with the stated rules,
    not pinned, and the two criteria reconciled sample by sample."""
    log = _drive_log()
    cal = calibrate_pedal_from_log(log)
    rows = _drive_rows()

    def evidence(rpm, load, mp):
        return rpm is not None and rpm >= 400 and (
            (load is not None and load >= 98) or (mp is not None and mp >= 95))
    fallback = {i for i, (rpm, _a, load, mp) in enumerate(rows)
                if rpm is not None and rpm >= 900 and evidence(rpm, load, mp)}
    learned = {i for i, (rpm, app, _l, _m) in enumerate(rows)
               if rpm is not None and app is not None
               and (app - cal.rest) / (cal.full - cal.rest) * 100 >= 75}
    assert analyze_log(log).wot_samples == len(fallback)
    log.pedal_cal = cal
    assert analyze_log(log).wot_samples == len(learned)
    assert learned
    # Pedal leads load and MAP: a pedal-WOT sample without load/MAP evidence
    # must be a tip-in, the very next row carrying the evidence. On this
    # drive that is row 193 (normalized pedal 87 %, load 90.6 %, MAP 88 kPa,
    # throttle 43.5 % and still opening; row 194: load 100 %, MAP 99). Every
    # other learned-calibration WOT sample has load >= 98 or MAP >= 95, and
    # all of them are in the fallback set.
    tip_ins = learned - fallback
    assert all(evidence(*(rows[i][0], rows[i][2], rows[i][3])) is False
               and i + 1 in fallback for i in tip_ins)
    assert tip_ins == {193}
    assert all(evidence(rows[i][0], rows[i][2], rows[i][3])
               for i in learned - tip_ins)


def test_drive_log_wot_under_fallback_and_under_learned_calibration():
    log = _drive_log()
    raw_app = log.series("app")
    assert max(v for v in raw_app if v is not None) < 70   # the old blind spot
    fallback = analyze_log(log)
    assert fallback.wot_criterion == BASIS_LOAD_MAP
    assert "pedal uncalibrated; WOT inferred from load/MAP" in \
        fallback.wot_criterion_text
    assert any("pedal uncalibrated" in n.lower() for n in fallback.notes)
    assert fallback.wot_samples == 50
    assert {r.name: r.n for r in fallback.regimes}[PE_REGIME] == 50
    assert any(s.wot for s in detect_shift_points(log))
    log.pedal_cal = calibrate_pedal_from_log(log)
    learned = analyze_log(log)
    assert learned.wot_criterion == BASIS_APP_CAL
    assert "rest 19.2 % / full 61.2 %" in learned.wot_criterion_text
    assert learned.wot_samples == 14
    assert "cruise" in {r.name for r in learned.regimes}
    assert any(s.wot for s in detect_shift_points(log))


def test_raw_app_without_a_calibration_never_drives_wot():
    text = ("Time (s),Engine Speed (RPM),Accelerator Pedal (%)\n"
            "0,3000,95\n0.5,3200,95\n")
    rep = analyze_log(parse_csv(text))
    assert rep.wot_samples == 0 and rep.wot_criterion == "none"
    assert "cannot be determined" in rep.wot_criterion_text


def _idle(lr, t0, values, step=0.5, max_gap=1.0):
    for i, v in enumerate(values):
        lr.observe(t0 + i * step, 700.0, 0.0, v, max_gap=max_gap)


def _pull(lr, t0, values, step=0.25, max_gap=1.0):
    for i, v in enumerate(values):
        lr.observe(t0 + i * step, 4000.0, 40.0, v, 100.0, 99.0,
                   max_gap=max_gap)


def test_learner_rules():
    lr = PedalLearner()
    # key on, engine off: MAP reads baro (99 kPa) - NOT WOT evidence
    for t in (0.0, 0.25, 0.5):
        lr.observe(t, 0.0, 0.0, 80.0, 0.0, 99.0)
    assert lr.cal.full is None
    lr.observe(0.75, 2000.0, 0.0, 30.0)                  # revving: no rest
    # stopped idle must hold 2 s before rest counts; rest is the median
    _idle(lr, 1.0, [19.6, 19.2, 19.4, 19.2])             # 1.0 .. 2.5 s
    assert lr.cal.rest is None
    lr.observe(3.0, 700.0, 0.0, 19.4)
    assert lr.cal.rest == 19.4 and not lr.cal.ready      # median of 5
    assert lr.cal.normalize(40.0) is None                # pending: no value
    _pull(lr, 4.0, [55.0, 61.0, 60.0])
    assert lr.cal.full == 61.0 and lr.cal.ready
    _pull(lr, 6.0, [50.0, 50.0, 50.0])
    assert lr.cal.full == 61.0                           # never lowered
    _idle(lr, 8.0, [20.0] * 6)
    assert lr.cal.rest == 19.4                           # never raised
    assert lr.cal.normalize(19.4) == 0.0
    assert lr.cal.normalize(61.0) == 100.0
    assert lr.cal.normalize(70.0) == 100.0               # clamped
    # a missing value breaks a rest stretch
    lr2 = PedalLearner()
    lr2.observe(0.0, 700.0, 0.0, 19.0)
    lr2.observe(1.0, None, 0.0, 19.0)
    lr2.observe(2.5, 700.0, 0.0, 18.0)
    assert lr2.cal.rest is None


def test_a_rest_glitch_and_a_wot_spike_do_not_move_the_calibration():
    lr = PedalLearner()
    _idle(lr, 0.0, [19.2, 19.2, 4.0, 19.2, 19.2, 19.2])  # one 4 % glitch
    assert lr.cal.rest == 19.2
    _pull(lr, 5.0, [61.0, 60.5, 61.2])
    assert lr.cal.full == 61.2
    lr.observe(7.0, 4500.0, 50.0, 95.0, 100.0, 99.0, max_gap=1.0)  # 1 spike
    lr.observe(7.25, 3000.0, 50.0, 30.0, 40.0, 50.0, max_gap=1.0)
    assert lr.cal.full == 61.2
    # two WOT samples are not a run either
    _pull(lr, 9.0, [90.0, 90.0])
    lr.observe(9.5, 3000.0, 50.0, 30.0, 40.0, 50.0, max_gap=1.0)
    assert lr.cal.full == 61.2


def test_rest_hold_needs_observed_continuity():
    lr = PedalLearner()
    lr.observe(0.0, 700.0, 0.0, 19.2, max_gap=0.5)
    lr.observe(2.5, 700.0, 0.0, 19.2, max_gap=0.5)       # 2.5 s unseen
    assert lr.cal.rest is None
    _idle(lr, 3.0, [19.2] * 3, step=0.5, max_gap=0.6)    # 3.0 .. 4.0
    lr.gap()                                             # a failed poll
    _idle(lr, 4.5, [19.2] * 4, step=0.5, max_gap=0.6)    # 4.5 .. 6.0
    assert lr.cal.rest is None                           # 1.5 s each side
    lr.observe(6.5, 700.0, 0.0, 19.2, max_gap=0.6)       # 2.0 s observed
    assert lr.cal.rest == 19.2


def test_reset_forgets_seed_learned_and_partial_state():
    lr = PedalLearner()
    lr.seed(PedalCal(19.0, 60.0, "settings"))
    _idle(lr, 0.0, [19.2] * 5)
    v = lr.version
    lr.reset()
    assert (lr.cal.rest, lr.cal.full, lr.cal.source) == (None, None, "")
    assert lr.learned_rest is None and lr.version > v
    _idle(lr, 10.0, [19.2] * 3)                          # 1 s: no rest yet
    assert lr.cal.rest is None


def test_from_dict_treats_nan_and_inf_as_pending():
    for bad in ("nan", float("inf"), "-inf"):
        cal = PedalCal.from_dict({"rest": bad, "full": 61.0})
        assert cal.rest is None and not cal.ready
    assert PedalCal.from_dict({"rest": "19.2", "full": "61.2"}).ready


def test_seed_merges_with_what_the_connection_learned():
    lr = PedalLearner()
    _pull(lr, 0.0, [63.0, 63.0, 63.0])                   # learned full 63
    lr.seed(PedalCal(19.2, 61.2, "settings:VIN"))
    assert (lr.cal.rest, lr.cal.full) == (19.2, 63.0)
    assert lr.cal.source == "settings:VIN + learned"


def _app_sample(src, t, req, **vals):
    rs = tuple(Reading(k, v, t, t + 0.05, req + i)
               for i, (k, v) in enumerate(vals.items()))
    return src._sample(rs)


def test_gt_derives_pedal_only_from_a_ready_calibration(tmp_path):
    src = GtDataSource(poll_interval=0)
    src._t0 = 0.0
    assert "pedal" in src.recordable_channels()
    s = _app_sample(src, 1.0, 1, rpm=700.0, vss=0.0, app=19.2)
    assert "pedal" not in s.values and s.derived is None  # pending
    for i in range(1, 6):
        src._publish(_app_sample(src, 1.0 + 0.5 * i, 10 + i, rpm=700.0,
                                 vss=0.0, app=19.2))
    assert src.pedal_calibration().rest == 19.2
    for i in range(2):
        src._publish(_app_sample(src, 4.0 + 0.5 * i, 20 + i, rpm=4000.0,
                                 vss=40.0, app=61.2, load=100.0, map=99.0))
    assert src.pedal_calibration().full is None          # 2 WOT: no run yet
    wot = _app_sample(src, 5.0, 30, rpm=4000.0, vss=40.0, app=61.2,
                      load=100.0, map=99.0)
    assert wot.values["pedal"] == 100.0 and wot.derived == {"pedal": "app"}
    assert wot.values["app"] == 61.2                      # raw kept as decoded
    assert all(r.key != "pedal" for r in wot.readings)    # not a request
    src._publish(wot)
    assert src.channel_values()["pedal"] == 100.0
    assert src.fresh_window_s("pedal") == src.fresh_window_s("app")
    half = _app_sample(src, 5.5, 40, rpm=2500.0, vss=40.0, app=40.2)
    assert half.values["pedal"] == pytest.approx(50.0)
    # recorded: its own CSV column, and its own line form in the sidecar
    rec = DriveRecorder(["rpm", "app", "pedal"], LABELS, [str(tmp_path)])
    rec.write([wot, half])
    rec.close()
    with open(rec.path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["Time (s)", "Engine Speed (RPM)",
                       "Accelerator Pedal (%)", "Pedal, normalized (%)"]
    assert rows[1][2:] == ["61.2", "100"] and rows[2][3] == "50"
    with open(rec.long_path, newline="", encoding="utf-8") as fh:
        long = list(csv.reader(fh))[1:]
    pedal_lines = [ln for ln in long if ln[1] == "pedal"]
    app_lines = [ln for ln in long if ln[1] == "app"]
    assert [ln[3] for ln in pedal_lines] == ["derived:app", "derived:app"]
    assert [ln[0] for ln in pedal_lines] == [ln[0] for ln in app_lines]
    assert all(ln[3].isdigit() for ln in long if ln[1] != "pedal")
    # and Log Analysis reads it back as the normalized pedal
    log = parse_csv(open(rec.path, encoding="utf-8").read())
    attach_long(log, open(rec.long_path, encoding="utf-8").read())
    assert log.series("pedal") == [100.0, 50.0]
    assert log.timed["pedal"][0][1] == 100.0
    assert analyze_log(log).wot_criterion == "pedal"


# --------------------------------------------------------------------------- #
# ethanol labelling
# --------------------------------------------------------------------------- #
def test_ethanol_is_labelled_an_estimate_and_old_headers_still_map():
    from openobd.app import CHANNEL_NOTES, RECORD_LABELS, RECORD_ORDER
    from openobd.gauges import BAR_SPECS, MODERN_RING_SPECS
    assert RECORD_LABELS["ethanol"] == "Ethanol, ECM estimate (%)"
    assert map_channel("Ethanol, ECM estimate (%)") == "ethanol"
    assert map_channel("Ethanol (%)") == "ethanol"
    assert map_channel("Pedal, normalized (%)") == "pedal"
    assert map_channel("Accelerator Pedal (%)") == "app"
    assert "ethanol" in RECORD_ORDER and "pedal" in RECORD_ORDER
    assert "ESTIMATE" in CHANNEL_NOTES["ethanol"]
    assert dict((s[0], s[1]) for s in BAR_SPECS)["ethanol"] == "EtOH est"
    assert dict((s[0], s[1]) for s in MODERN_RING_SPECS)["ethanol"] == "ETOH EST"
    log = _drive_log()                     # recorded with the OLD header
    assert log.has("ethanol")


# --------------------------------------------------------------------------- #
# Dashboard: marker shortcut, metadata at start, Pedal gauge
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qapp(tmp_path_factory):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(tmp_path_factory.mktemp("meta_settings")))
    return QApplication.instance() or QApplication([])


class FakeGt(GtDataSource):
    """A GtDataSource fed by the test, on a virtual clock."""

    def __init__(self, ctx_gt=None):
        super().__init__(port="COM5")
        self._gt = ctx_gt or SimpleNamespace(
            close=lambda: None, supported=set(), port_name="COM5",
            possible_keys=lambda: ["rpm", "vss", "app", "load", "map"])
        self._t0 = 0.0
        self.now = 0.0
        self._thread = object()            # live: only a live GT records

    def clock(self):
        return self.now

    def feed(self, t, req, **vals):
        self.now = t + 0.1
        self._publish(_app_sample(self, t, req, **vals))

    def stop(self):
        pass


def _settings_groups():
    from openobd.appsettings import app_settings
    st = app_settings()
    st.beginGroup("pedal_cal")
    try:
        return set(st.childGroups())
    finally:
        st.endGroup()


@pytest.fixture
def dash(qapp, tmp_path):
    from openobd.app import Dashboard
    from openobd.appsettings import app_settings
    app_settings().clear()                    # each test starts unstored
    d = Dashboard()
    d._record_dirs = lambda: [str(tmp_path / "Logs")]
    src = FakeGt()
    d.bind_source(src)
    yield d, src
    d.timer.stop()


def test_marker_shortcut_is_registered_and_marks_while_recording(dash):
    d, src = dash
    assert d.shortcut_tooltip(d.btn_mark).endswith("(M)")
    assert not d.btn_mark.isEnabled()
    d.add_marker()
    assert "while recording" in d.status.text()
    d.toggle_record()
    assert d.recording and d.btn_mark.isEnabled()
    rec = d._recorder
    meta = read_meta(rec.path)
    assert meta["source"]["kind"] == "gt" and meta["source"]["port"] == "COM5"
    assert "poll_cost_model" in meta["source"]
    assert meta["vehicle"]["examined"] is False
    assert meta["pedal_calibration"]["at_start"]["ready"] is False
    assert "pedal" in meta["derived_channels"]
    assert meta["columns"]["pedal"] == "Pedal, normalized (%)"
    src.feed(1.0, 1, rpm=700.0, vss=0.0, app=19.2)
    d._tick()
    src.now = 3.0
    d.btn_mark.click()
    assert "mark 1 at 2.0 s" in d.status.text()
    d._finalize_record()
    meta = read_meta(rec.path)
    assert meta["markers"] == [{"t": 1.975, "label": "mark 1"}]
    assert meta["rows"] == 1
    assert not d.btn_mark.isEnabled()


def test_recording_context_lands_in_the_metadata(dash, timed_gt):
    d, _old = dash
    g, clock = timed_gt(pids=TRUCK_PIDS, info=INFO)
    src = FakeGt(ctx_gt=g)
    d.bind_source(src)
    src.feed(1.0, 1, rpm=700.0, vss=0.0, app=19.2)
    d.toggle_record()
    rec = d._recorder
    ctx = src.read_recording_context()        # what the worker thread runs
    d._on_record_context((src, rec, ctx))
    meta = read_meta(rec.path)
    assert meta["vehicle"]["vin"]["value"] == VIN
    assert meta["dtcs_at_start"]["examined"] is False
    assert meta["supported_pids"]["pids"] == sorted(TRUCK_PIDS)
    assert d._pedal_key == VIN and VIN in d.status.text()
    assert meta["pedal_calibration"]["persistence"] == f"settings pedal_cal/{VIN}"
    d._finalize_record()


def _calibrate_live(src, t0=1.0, req=100):
    """Feed a 2.5 s stopped idle then a 3-sample pull at the poll rate."""
    for i in range(6):
        src.feed(t0 + 0.5 * i, req + i, rpm=700.0, vss=0.0, app=19.2)
    for i in range(3):
        src.feed(t0 + 3.5 + 0.25 * i, req + 10 + i, rpm=4000.0, vss=40.0,
                 app=61.2, load=100.0, map=99.0)
    return t0 + 4.5


def test_pedal_gauge_shows_raw_until_calibrated_then_normalized(dash):
    d, src = dash
    g = d.gauges["app"]
    src.feed(0.5, 1, rpm=700.0, vss=0.0, app=19.2)
    d._tick()
    assert g.label == "Pedal raw" and g.value == 19.2
    assert "pedal cal pending" in g.toolTip()
    t = _calibrate_live(src)
    src.feed(t, 200, rpm=2500.0, vss=40.0, app=40.2, load=60.0, map=60.0)
    d._tick()
    assert g.label == "Pedal" and g.value == pytest.approx(50.0)


def test_without_a_vin_nothing_is_persisted_or_seeded(dash, tmp_path):
    from openobd.app import save_pedal_cal
    d, src = dash
    save_pedal_cal("1GCEK19J78Z123456", PedalCal(10.0, 90.0, "other truck"))
    d._sync_link()
    assert d._pedal_key is None and src.vin() is None
    assert src.pedal_calibration().rest is None          # nothing seeded
    d.toggle_record()
    rec = d._recorder
    _calibrate_live(src)
    d._tick()
    assert src.pedal_calibration().ready                 # live learning works
    assert _settings_groups() == {"1GCEK19J78Z123456"}   # nothing new stored
    assert d._meta_timer.isActive()                      # rewrite coalesced
    d._flush_meta()                                      # what 250 ms does
    pc = read_meta(rec.path)["pedal_calibration"]
    assert pc["persistence"].startswith("session-only")
    assert pc["changes"] and pc["changes"][-1]["ready"]
    assert pc["vin"] is None
    d._finalize_record()


def test_a_vin_connection_seeds_only_its_own_and_saves_at_once(dash, timed_gt):
    from openobd.app import load_pedal_cal, save_pedal_cal
    d, _old = dash
    g, _clock = timed_gt(pids=TRUCK_PIDS, info=INFO)
    src = FakeGt(ctx_gt=g)
    src._new_link()                                      # what start() does
    save_pedal_cal(VIN, PedalCal(19.6, 60.0, f"settings:{VIN}"))
    d.bind_source(src)
    d._bind_gt(src)
    d.timer.stop()
    assert d._pedal_key == VIN and f"VIN {VIN}" in d.status.text()
    cal = src.pedal_calibration()
    assert (cal.rest, cal.full) == (19.6, 60.0)          # seeded at connect
    _calibrate_live(src)
    d._tick()
    stored = load_pedal_cal(VIN)
    assert (stored.rest, stored.full) == (19.2, 61.2)    # saved on change
    assert sum(1 for c, *_ in g.ser.exchanges if c == "0902") == 1


def test_reconnect_to_another_truck_resets_and_never_cross_applies(
        dash, timed_gt):
    from openobd.app import load_pedal_cal, save_pedal_cal
    d, _old = dash
    g, _clock = timed_gt(pids=TRUCK_PIDS, info=INFO)
    g.open = g.close = lambda: None                      # no serial port
    src = FakeGt(ctx_gt=g)
    src._new_link()
    d.bind_source(src)
    d._bind_gt(src)
    d.timer.stop()
    d.toggle_record()
    rec = d._recorder
    t = _calibrate_live(src)
    d._tick()
    assert d._pedal_key == VIN and load_pedal_cal(VIN).ready
    vin_b = "1GCEK19J78Z123456"
    save_pedal_cal(vin_b, PedalCal(25.0, 80.0, f"settings:{vin_b}"))
    g.ser.info = dict(INFO, **{"02": b"\x49\x02\x01" + vin_b.encode()})
    src._fails_since_open = src.RECONNECT_AFTER
    src._reconnect()                                     # truck B now
    assert src._identity is None and src.vin() == vin_b
    assert src.pedal.learned_rest is None
    d._tick()
    assert d._pedal_key == vin_b
    cal = src.pedal_calibration()
    assert (cal.rest, cal.full) == (25.0, 80.0)          # B's own, not A's
    a = load_pedal_cal(VIN)
    assert (a.rest, a.full) == (19.2, 61.2)              # A untouched
    src.feed(t + 1.0, 500, rpm=4000.0, vss=40.0, app=70.0, load=100.0,
             map=99.0)
    d._tick()
    assert load_pedal_cal(VIN).full == 61.2              # never saved to A
    d._flush_meta()
    events = read_meta(rec.path)["link_events"]
    assert events[-1]["vin"]["value"] == vin_b
    d._finalize_record()


def test_stop_and_start_reset_the_learner_and_reread_the_vin(timed_gt):
    g, _clock = timed_gt(pids=TRUCK_PIDS, info=INFO)
    g.open = lambda: None
    src = GtDataSource(poll_interval=0)
    src._gt = g
    src.pedal.seed(PedalCal(19.0, 60.0, "left over"))
    src._interval = 0.01
    epoch = src.link[0]
    src.start()
    try:
        assert src.link[0] == epoch + 1 and src.vin() == VIN
        assert src.pedal_calibration().source != "left over"
    finally:
        src.stop()
    assert src.vin() is None and src.link[0] == epoch + 2
    assert src.pedal_calibration().rest is None and src._identity is None


def test_a_replayed_log_without_pedal_shows_raw_app(dash):
    from openobd.transport import LogReplaySource
    d, _src = dash
    log = _drive_log()
    rs = LogReplaySource(log)
    d.bind_source(rs)
    rs.start()
    d._tick()
    assert d.gauges["app"].label == "Pedal raw"
    assert d.gauges["app"].value == pytest.approx(19.2157)
    rs.stop()


def _log_with_meta(meta):
    log = parse_csv("Time (s),Engine Speed (RPM),Accelerator Pedal (%)\n"
                    "0,800,19.2\n")
    log.meta = meta
    return log


def _cal(rest, full, src):
    return {"rest": rest, "full": full, "source": src}


def test_known_pedal_cal_order_and_vin_gate(dash):
    from openobd.app import MainWindow, save_pedal_cal
    known = lambda log: MainWindow._known_pedal_cal(MainWindow, log)  # noqa: E731
    pc = {"at_start": _cal(19.0, 60.0, "start"),
          "after_identity": _cal(19.1, 60.5, "identity"),
          "changes": [_cal(19.2, 61.0, "c1"), _cal(19.2, 61.2, "c2"),
                      {"rest": None, "full": None, "source": "reset"}]}
    vin = {"vin": {"examined": True, "value": VIN}}
    assert known(_log_with_meta({"pedal_calibration": pc})).source == "c2"
    pc2 = dict(pc, changes=[])
    assert known(_log_with_meta({"pedal_calibration": pc2})).source == \
        "identity"
    pc3 = {"at_start": _cal(19.0, 60.0, "start")}
    assert known(_log_with_meta({"pedal_calibration": pc3})).source == "start"
    # a stored calibration is NEVER loaded on a file's say-so (SC-05): the
    # VIN in a .meta.json is unverified
    save_pedal_cal(VIN, PedalCal(18.0, 65.0, "stored"))
    assert known(_log_with_meta(None)) is None
    assert known(_log_with_meta({"vehicle": {"vin": {
        "examined": False, "why": "NO DATA"}}})) is None
    assert known(_log_with_meta({"vehicle": vin})) is None
    # a log uses what its own recording measured, VIN claimed or not
    assert known(_log_with_meta({"vehicle": vin,
                                 "pedal_calibration": pc})).source == "c2"


def _live(vin):
    """A dashboard bound to a GT connection whose VIN read as `vin`."""
    src = FakeGt()
    src.link = (1, {"examined": True, "value": vin} if vin else None)
    return SimpleNamespace(source=src, _pedal_key=vin,
                           _pedal_store_error=None)


def _learn_window(log, dashboard=None):
    from openobd.app import MainWindow
    msgs = []
    ns = SimpleNamespace(
        log=log, _log_vin=MainWindow._log_vin,
        statusBar=lambda: SimpleNamespace(
            showMessage=lambda m, *_a: msgs.append(m)),
        log_report=SimpleNamespace(append=lambda m: msgs.append(m)),
        _analyze_loaded_log=lambda: None,
        dashboard=dashboard or SimpleNamespace(source=None, _pedal_key=None))
    MainWindow.learn_pedal_from_log(ns)
    return msgs


def test_learn_merges_and_never_loosens_a_stored_calibration(dash):
    from openobd.app import load_pedal_cal, save_pedal_cal
    log = _drive_log()
    log.meta = {"vehicle": {"vin": {"examined": True, "value": VIN}}}
    save_pedal_cal(VIN, PedalCal(18.0, 65.0, "stored"))
    msgs = _learn_window(log, _live(VIN))
    stored = load_pedal_cal(VIN)
    assert (stored.rest, stored.full) == (18.0, 65.0)    # not loosened
    assert "was rest 18.0 % / full 65.0 %" in msgs[0]
    assert "learned rest 19.2 % / full 61.2 %" in msgs[0]
    save_pedal_cal(VIN, PedalCal(20.0, 55.0, "stored"))
    _learn_window(log, _live(VIN))
    stored = load_pedal_cal(VIN)
    assert (round(stored.rest, 1), round(stored.full, 1)) == (19.2, 61.2)


def test_a_doctored_meta_vin_neither_loads_nor_writes_that_vins_settings(
        dash, tmp_path):
    """SC-05: a .meta.json naming another VIN must not pull that VIN's
    stored calibration into the analysis, and Learn must not widen it."""
    from openobd.app import MainWindow, load_pedal_cal, save_pedal_cal
    victim = "1GCEK19J78Z123456"
    save_pedal_cal(victim, PedalCal(30.0, 50.0, "victim's own"))
    log = _drive_log()
    log.meta = {"schema": 1,
                "vehicle": {"vin": {"examined": True, "value": victim}}}
    assert MainWindow._known_pedal_cal(MainWindow, log) is None
    for live in (None, _live(VIN)):        # not connected / another truck
        msgs = _learn_window(log, live)
        assert "applies to this log's analysis only" in msgs[0]
        assert "from file, unverified" in msgs[0]
        assert log.pedal_cal.ready         # the analysis still gets it
        stored = load_pedal_cal(victim)
        assert (stored.rest, stored.full) == (30.0, 50.0)
    assert _settings_groups() == {victim}


def test_an_unreadable_settings_store_is_never_written_over(dash, timed_gt,
                                                            monkeypatch):
    import openobd.app as appmod
    from openobd.app import save_pedal_cal
    d, _old = dash
    save_pedal_cal(VIN, PedalCal(18.0, 65.0, "real stored value"))
    saved = []
    monkeypatch.setattr(appmod, "load_pedal_cal",
                        lambda vin: (_ for _ in ()).throw(OSError("hive locked")))
    monkeypatch.setattr(appmod, "save_pedal_cal",
                        lambda vin, cal: saved.append((vin, cal)))
    g, _clock = timed_gt(pids=TRUCK_PIDS, info=INFO)
    src = FakeGt(ctx_gt=g)
    src._new_link()
    d.bind_source(src)
    d._bind_gt(src)
    d.timer.stop()
    assert "unreadable" in d.status.text() and "hive locked" in d.status.text()
    assert src.pedal_calibration().rest is None          # nothing seeded
    d.toggle_record()
    rec = d._recorder
    _calibrate_live(src)
    d._tick()
    assert src.pedal_calibration().ready and saved == []  # never saved over
    d._flush_meta()
    pc = read_meta(rec.path)["pedal_calibration"]
    assert "UNREADABLE" in pc["persistence"] and "hive locked" in pc["persistence"]
    # Learn for the same live VIN refuses to store too
    log = _drive_log()
    log.meta = {"vehicle": {"vin": {"examined": True, "value": VIN}}}
    msgs = _learn_window(log, d)
    assert "unreadable" in msgs[0] and saved == []
    d._finalize_record()


@pytest.mark.parametrize("doc, why", [
    ({"schema": 1, "vehicle": ["3GCRKTE35AG150432"]}, "vehicle is not an object"),
    ({"schema": 1, "markers": [{"t": "12.5", "label": "x"}]},
     "a marker is not {t: number, label: text}"),
    ({"schema": 1, "vehicle": {"vin": {"examined": "yes", "value": VIN}}},
     "vehicle.vin.examined"),
    ({"schema": 1, "pedal_calibration": {"changes": "lots"}},
     "pedal_calibration.changes"),
    ([1, 2], "not a JSON object"),
])
def test_hostile_meta_shapes_are_reported_not_crashes(tmp_path, doc, why):
    csv_path = tmp_path / "x.csv"
    csv_path.write_text("Time (s),Engine Speed (RPM)\n0,800\n")
    (tmp_path / "x.meta.json").write_text(json.dumps(doc))
    with pytest.raises(ValueError, match=re.escape(why)):
        read_meta(str(csv_path))


def test_a_hostile_meta_still_lets_the_log_load(qapp, tmp_path):
    from openobd.app import MainWindow, load_default_cal
    csv_path = tmp_path / "x.csv"
    csv_path.write_text("Time (s),Engine Speed (RPM),Accelerator Pedal (%)\n"
                        "0,800,19.2\n0.5,900,19.2\n")
    (tmp_path / "x.meta.json").write_text(json.dumps(
        {"schema": 1, "vehicle": [], "pedal_calibration": [],
         "markers": [{"t": "soon"}]}))
    cal, path = load_default_cal()
    win = MainWindow(cal, path)
    try:
        win.load_log_path(str(csv_path))
        assert win.log is not None and win.log.n_samples == 2
        assert win.log.meta is None and win._marker_times == []
        assert "metadata unreadable: vehicle is not an object" in \
            win.log_report.toPlainText()
    finally:
        win.dashboard.shutdown()
        win.close()


def test_held_m_coalesces_marker_rewrites(dash, monkeypatch):
    d, src = dash
    d.toggle_record()
    rec = d._recorder
    src.feed(1.0, 1, rpm=700.0, vss=0.0, app=19.2)
    d._tick()
    writes = []
    real = rec._write_meta
    monkeypatch.setattr(rec, "_write_meta", lambda: writes.append(1) or real())
    for i in range(20):                                  # auto-repeat
        src.now = 2.0 + i * 0.03
        d.add_marker()
    assert writes == [] and d._meta_timer.isActive()
    d._flush_meta()
    assert len(writes) == 1 and len(read_meta(rec.path)["markers"]) == 20
    d._finalize_record()
    assert len(read_meta(rec.path)["markers"]) == 20


def test_learn_on_a_vin_less_log_is_analysis_only(dash):
    log = _drive_log()
    msgs = _learn_window(log)
    assert "No VIN in this log; calibration applies to this log's analysis " \
        "only" in msgs[0]
    assert log.pedal_cal.ready and _settings_groups() == set()


def test_sidecar_meta_reload_helpers(tmp_path):
    rec = _rec(tmp_path)
    rec.write([_sample(10.0, 1, rpm=800.0)])
    rec.add_marker(10.5, "pull start")
    rec.close()
    assert os.path.exists(sidecar_path(rec.path))
    with open(meta_path(rec.path), encoding="utf-8") as fh:
        assert json.load(fh)["markers"][0]["label"] == "pull start"
    bad = tmp_path / "x.csv"
    bad.write_text("Time (s)\n")
    (tmp_path / "x.meta.json").write_text("[1, 2]")
    with pytest.raises(ValueError):
        read_meta(str(bad))
    assert read_meta(str(tmp_path / "none.csv")) is None


def test_rules_constants_are_the_brief():
    assert (pedalmod.REST_RPM_MAX, pedalmod.REST_HOLD_S,
            pedalmod.WOT_LOAD_MIN, pedalmod.WOT_MAP_MIN) == (1200, 2.0, 98, 95)
