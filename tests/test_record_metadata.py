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


def test_learner_rules():
    lr = PedalLearner()
    # key on, engine off: MAP reads baro (99 kPa) - NOT WOT evidence
    lr.observe(0.0, 0.0, 0.0, 80.0, 0.0, 99.0)
    assert lr.cal.full is None
    lr.observe(0.5, 2000.0, 0.0, 30.0)                   # revving: no rest
    # stopped idle must hold 2 s before rest counts
    lr.observe(1.0, 700.0, 0.0, 19.6)
    lr.observe(2.5, 700.0, 0.0, 19.2)
    assert lr.cal.rest is None
    lr.observe(3.0, 700.0, 0.0, 19.4)
    assert lr.cal.rest == 19.2 and not lr.cal.ready
    assert lr.cal.normalize(40.0) is None                # pending: no value
    lr.observe(4.0, 3500.0, 30.0, 61.0, 100.0, 99.0)
    assert lr.cal.full == 61.0 and lr.cal.ready
    lr.observe(5.0, 3500.0, 30.0, 50.0, 100.0, 99.0)
    assert lr.cal.full == 61.0                           # never lowered
    assert lr.cal.normalize(19.2) == 0.0
    assert lr.cal.normalize(61.0) == 100.0
    assert lr.cal.normalize(70.0) == 100.0               # clamped
    # a missing value breaks a rest stretch
    lr2 = PedalLearner()
    lr2.observe(0.0, 700.0, 0.0, 19.0)
    lr2.observe(1.0, None, 0.0, 19.0)
    lr2.observe(2.5, 700.0, 0.0, 18.0)
    assert lr2.cal.rest is None


def test_seed_merges_with_what_the_connection_learned():
    lr = PedalLearner()
    lr.observe(0.0, 3500.0, 30.0, 63.0, 100.0, 99.0)     # learned full 63
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
    for i, t in enumerate((2.0, 3.5)):
        src._publish(_app_sample(src, t, 10 + i, rpm=700.0, vss=0.0, app=19.2))
    assert src.pedal_calibration().rest == 19.2
    wot = _app_sample(src, 5.0, 20, rpm=4000.0, vss=40.0, app=61.2,
                      load=100.0, map=99.0)
    assert wot.values["pedal"] == 100.0 and wot.derived == {"pedal": "app"}
    assert wot.values["app"] == 61.2                      # raw kept as decoded
    assert all(r.key != "pedal" for r in wot.readings)    # not a request
    src._publish(wot)
    assert src.channel_values()["pedal"] == 100.0
    assert src.fresh_window_s("pedal") == src.fresh_window_s("app")
    half = _app_sample(src, 6.0, 30, rpm=2500.0, vss=40.0, app=40.2)
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

    def clock(self):
        return self.now

    def feed(self, t, req, **vals):
        self.now = t + 0.1
        self._publish(_app_sample(self, t, req, **vals))

    def stop(self):
        pass


@pytest.fixture
def dash(qapp, tmp_path):
    from openobd.app import Dashboard
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
    d._finalize_record()


def test_pedal_gauge_shows_raw_until_calibrated_then_normalized(dash):
    from openobd.app import load_pedal_cal
    d, src = dash
    g = d.gauges["app"]
    src.feed(1.0, 1, rpm=700.0, vss=0.0, app=19.2)
    d._tick()
    assert g.label == "Pedal raw" and g.value == 19.2
    assert "pedal cal pending" in g.toolTip()
    src.feed(2.0, 5, rpm=700.0, vss=0.0, app=19.2)
    src.feed(3.5, 9, rpm=700.0, vss=0.0, app=19.2)       # rest learned
    d._tick()
    assert g.label == "Pedal raw"                         # full still pending
    src.feed(5.0, 13, rpm=4000.0, vss=40.0, app=61.2, load=100.0, map=99.0)
    src.feed(6.0, 18, rpm=2500.0, vss=40.0, app=40.2, load=60.0, map=60.0)
    d._tick()
    assert g.label == "Pedal" and g.value == pytest.approx(50.0)
    stored = load_pedal_cal("unknown-vin")               # not identified
    assert (stored.rest, stored.full) == (19.2, 61.2)


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
