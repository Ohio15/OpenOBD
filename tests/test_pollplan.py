"""Batched, tiered GT polling (v0.33.0): the plan, the multi-PID reply
parser, and Log Analysis reading the long-format sidecar.

The 2026-10-25 drive (gt_log_20261025_141431.csv) polled 22 PIDs one request
at a time at ~63 ms each: 0.69 Hz. SAE J1979 Mode 01 on CAN carries up to six
PIDs per request; the E38 answers with one (multi-frame) ISO-TP message.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import gt as gtmod, pollplan  # noqa: E402
from openobd.gt import mode01_message, parse_mode01_reply  # noqa: E402
from openobd.logbin import (  # noqa: E402
    analyze_log, attach_long, detect_shift_points, map_channel, parse_csv,
    parse_long_csv)

LENGTHS = {pid: d.nbytes for pid, d in gtmod.PID_TABLE.items()}
KEYS = {pid: d.key for pid, d in gtmod.PID_TABLE.items()}


# --------------------------------------------------------------------------- #
# PID table
# --------------------------------------------------------------------------- #
def test_pid_lengths_are_the_j1979_lengths():
    want = {"03": 2, "04": 1, "05": 1, "06": 1, "07": 1, "08": 1, "09": 1,
            "0A": 1, "0B": 1, "0C": 2, "0D": 1, "0E": 1, "0F": 1, "10": 2,
            "11": 1, "14": 2, "15": 2, "18": 2, "19": 2, "1F": 2, "2F": 1,
            "33": 1, "3C": 2, "3D": 2, "42": 2, "43": 2, "44": 4, "45": 1,
            "46": 1, "47": 1, "49": 1, "4A": 1, "4C": 1, "52": 1}
    assert LENGTHS == want


@pytest.mark.parametrize("pid, data, want", [
    ("08", [0x90], 12.5),                       # stft_b2
    ("09", [0x70], -12.5),                      # ltft_b2
    ("14", [0x5A, 0xFF], 0.45),                 # O2 B1S1 V; byte1 ignored
    ("19", [0xB4, 0x80], 0.9),
    ("3C", [0x1F, 0x40], (800 - 40) * 9 / 5 + 32),   # 8000/10-40 C -> F
    ("03", [0x02, 0x00], 2.0),                  # closed loop, raw code
    ("43", [0x00, 0xFF], 100.0),                # abs load %
    ("45", [0xFF], 100.0),
    ("47", [0x33], 20.0),
    ("4A", [0x66], 40.0),
])
def test_new_pid_decoders(pid, data, want):
    assert gtmod.PID_TABLE[pid].decode(data) == pytest.approx(want)


def test_new_channels_have_columns_and_reload_as_themselves():
    from openobd.app import RECORD_LABELS
    for key in ("stft_b2", "ltft_b2", "o2_b1s1", "o2_b1s2", "o2_b2s1",
                "o2_b2s2", "cat_b1s1", "cat_b2s1", "fuel_system", "abs_load",
                "rel_tps", "app_e", "app_f"):
        assert map_channel(RECORD_LABELS[key]) == key
    # the generic channels still resolve to themselves
    assert map_channel("Engine Load (%)") == "load"
    assert map_channel("Throttle Position (%)") == "tps"
    assert map_channel("Accelerator Pedal (%)") == "app"
    assert map_channel("Short Term Fuel Trim (%)") == "stft"
    assert map_channel("Module Voltage (V)") == "voltage"


# --------------------------------------------------------------------------- #
# reply parser
# --------------------------------------------------------------------------- #
def test_multi_frame_reply_strips_length_line_indices_and_padding():
    # 01 0C 0D 11 49 0B 10 -> 41 0C 1A F8 0D 32 11 40 49 33 0B 21 10 01 90
    # (15 bytes = 0x00F), last frame padded with 00.
    resp = "00F 0:410C1AF80D32 1:114049330B2110 2:01900000000000"
    msg = mode01_message(resp)
    assert len(msg) == 15 and msg[0] == 0x41 and msg[-2:] == [0x01, 0x90]
    got = parse_mode01_reply(resp, ["0C", "0D", "11", "49", "0B", "10"],
                             LENGTHS)
    assert got == {"0C": [0x1A, 0xF8], "0D": [0x32], "11": [0x40],
                   "49": [0x33], "0B": [0x21], "10": [0x01, 0x90]}


def test_the_old_hex_strip_would_have_corrupted_that_reply():
    resp = "00F 0:410C1AF80D32 1:114049330B2110 2:01900000000000"
    # what _extract does: the length line and indices become 'data'
    assert gtmod.ObdxGt._extract(resp, "010C") != [0x1A, 0xF8]


def test_single_frame_and_spaces_on_and_headers_on():
    assert parse_mode01_reply("410C1AF8", ["0C"], LENGTHS) == {"0C": [0x1A, 0xF8]}
    spaced = "00A 0: 41 0C 1A F8 0D 32 1: 11 40 49 33 00 00 00"
    assert parse_mode01_reply(spaced, ["0C", "0D", "11", "49"], LENGTHS) == {
        "0C": [0x1A, 0xF8], "0D": [0x32], "11": [0x40], "49": [0x33]}
    headered = "7E8100A410C1AF80D32 7E82111404933000000"
    assert parse_mode01_reply(headered, ["0C", "0D", "11", "49"], LENGTHS) == {
        "0C": [0x1A, 0xF8], "0D": [0x32], "11": [0x40], "49": [0x33]}


def test_no_data_negative_response_and_frame_gap():
    assert parse_mode01_reply("NO DATA", ["0C", "0D"], LENGTHS) == {}
    assert parse_mode01_reply("7F0112", ["0C", "0D"], LENGTHS) == {}
    assert parse_mode01_reply("SEARCHING... UNABLE TO CONNECT", ["0C"], LENGTHS) == {}
    # frame 1 lost: only what frame 0 fully carries survives
    gap = "00F 0:410C1AF80D32 2:01900000000000"
    assert parse_mode01_reply(gap, ["0C", "0D", "11"], LENGTHS) == {
        "0C": [0x1A, 0xF8], "0D": [0x32]}


def test_walk_stops_at_padding_and_unrequested_pids():
    # A 00 pad must never be read as PID 00 (truck-mcp parse_multi_pid).
    assert parse_mode01_reply("410D32000000", ["0D", "0C"], LENGTHS) == {
        "0D": [0x32]}
    # a PID cut short is dropped, not padded out
    assert parse_mode01_reply("410D320C1A", ["0D", "0C"], LENGTHS) == {
        "0D": [0x32]}


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_every_pid_channel_has_a_tier():
    assert set(KEYS.values()) <= set(pollplan.KEY_TIERS)


def test_request_cost_model_is_the_measured_points():
    assert pollplan.request_cost_s(1) == pytest.approx(0.0557)
    assert pollplan.request_cost_s(3) == pytest.approx(0.0550)
    assert pollplan.request_cost_s(6) == pytest.approx(0.0799)
    assert pollplan.request_cost_s(5) == pytest.approx(0.055 + 2 * 0.0249 / 3)


def test_plan_for_this_trucks_supported_set():
    plan = pollplan.PollPlan.build(KEYS, KEYS.keys())
    s = plan.summary()
    assert len(s["tiers"]["fast"]) == 17
    assert len(s["tiers"]["medium"]) == 9
    assert len(s["tiers"]["slow"]) == 8
    assert s["batches"]["fast"][0] == ["rpm", "vss", "map", "maf", "tps", "app"]
    assert [len(b) for b in s["batches"]["fast"]] == [6, 6, 5]
    assert s["requests"] == {"fast": 3, "medium": 5, "slow": 6}
    assert s["predicted_s"]["fast"] == pytest.approx(0.2314, abs=1e-4)
    assert s["predicted_s"]["fast"] <= 0.25
    assert s["mean_cycle_s"] == pytest.approx(0.26995, abs=1e-4)
    assert not s["atrv_voltage"]                    # PID 42 is supported
    # every PID appears exactly once over a SLOW period's first cycle
    assert sorted(p for b in plan.batches_for_cycle(0) for p in b) == sorted(KEYS)


def test_only_supported_pids_are_planned_and_atrv_covers_missing_42():
    plan = pollplan.PollPlan.build(KEYS, {"0C", "05", "33"})
    assert sorted(plan.pids) == ["05", "0C", "33"]
    assert plan.atrv and "voltage" in plan.summary()["tiers"]["slow"]
    assert plan.batches_for_cycle(1) == [["0C"]]
    assert plan.batches_for_cycle(4) == [["0C", "05"]]
    assert plan.batches_for_cycle(20) == [["0C", "05", "33"]]
    assert plan.atrv_due(20) and not plan.atrv_due(4)


def test_plan_is_rebuilt_when_supported_changes():
    g = gtmod.ObdxGt(port="X")
    g.supported = {"0C"}
    p1 = g.poll_plan()
    assert g.poll_plan() is p1
    g._cycle = 7
    g.supported = {"0C", "0D"}
    p2 = g.poll_plan()
    assert p2 is not p1 and sorted(p2.pids) == ["0C", "0D"] and g._cycle == 0


# --------------------------------------------------------------------------- #
# Log Analysis: the long-format sidecar
# --------------------------------------------------------------------------- #
WIDE = ("Time (s),Engine Speed (RPM),Vehicle Speed (mph),Accelerator Pedal (%),"
        "Short Term Fuel Trim (%),Long Term Fuel Trim (%)\n")


def _long(rows):
    head = "Time (s),Channel,Value,Request,Sweep Start (s),Sweep End (s)\n"
    return head + "".join(f"{t},{k},{v},1,0,0\n" for t, k, v in rows)


def test_parse_long_csv_groups_and_sorts_per_channel():
    td = parse_long_csv(_long([(0.2, "rpm", 900), (0.1, "rpm", 800),
                               (0.15, "ltft", 2.5), (0.3, "rpm", "bad")]))
    assert td == {"rpm": [(0.1, 800.0), (0.2, 900.0)], "ltft": [(0.15, 2.5)]}
    with pytest.raises(ValueError):
        parse_long_csv("Time (s),Engine Speed (RPM)\n0,800\n")


def test_regimes_use_each_trim_readings_own_time():
    # Wide rows say cruise throughout (pedal 10 %). The sidecar shows the
    # ONLY ltft reading was taken while the pedal was at WOT, 0.2 s after
    # the row's midpoint: per-channel times classify it as PE, not cruise.
    wide = WIDE + "0.0,2000,40,10,1.0,5.0\n1.0,2000,40,10,1.0,\n"
    log = parse_csv(wide)
    plain = {r.name: r for r in analyze_log(log).regimes}
    assert plain["cruise"].ltft_mean == pytest.approx(5.0)
    side = _long([(0.0, "rpm", 2000), (0.0, "app", 10), (0.0, "stft", 1.0),
                  (0.3, "app", 90), (0.3, "rpm", 2600), (0.32, "ltft", 5.0),
                  (1.0, "rpm", 2000), (1.0, "app", 10), (1.0, "stft", 1.0)])
    attach_long(log, side)
    timed = {r.name: r for r in analyze_log(log).regimes}
    assert timed["cruise"].ltft_mean is None
    assert timed["PE (WOT/high load)"].ltft_mean == pytest.approx(5.0)
    assert timed["cruise"].n == 2 and timed["cruise"].stft_mean == pytest.approx(1.0)


def test_shift_detection_uses_rpm_readings_at_their_own_times():
    wide = (WIDE + "0.0,5400,50,90,0,0\n0.25,5450,52,90,0,0\n"
            "0.5,3600,53,90,0,0\n")
    log = parse_csv(wide)
    assert [e.time_s for e in detect_shift_points(log)] == [0.5]
    attach_long(log, _long([
        (0.01, "rpm", 5400), (0.02, "vss", 50), (0.03, "app", 90),
        (0.26, "rpm", 5450), (0.27, "vss", 52), (0.28, "app", 90),
        (0.51, "rpm", 3600), (0.52, "vss", 53), (0.53, "app", 90)]))
    ev = detect_shift_points(log)
    assert len(ev) == 1 and ev[0].time_s == pytest.approx(0.51)
    assert ev[0].rpm == 5450 and ev[0].wot and ev[0].inferred


def test_a_recorded_tiered_drive_reloads_with_its_sidecar(tmp_path):
    from openobd.app import RECORD_LABELS
    from openobd.recorder import DriveRecorder
    from openobd.transport import Reading, Sample
    rec = DriveRecorder(["rpm", "app", "stft", "ltft"], RECORD_LABELS,
                        [str(tmp_path)])
    req = 0
    for i in range(8):
        t = i * 0.25
        vals = {"rpm": 2000.0, "app": 10.0, "stft": 1.0}
        if i % 4 == 0:
            vals["ltft"] = 3.0                      # MEDIUM tier
        req += 1
        rs = tuple(Reading(k, v, t, t + 0.08, req) for k, v in vals.items())
        rec.write([Sample(t + 0.04, vals, rs)])
    rec.close()
    log = parse_csv(open(rec.path, encoding="utf-8").read())
    assert log.series("ltft")[1] is None            # blank = not sampled
    attach_long(log, open(rec.long_path, encoding="utf-8").read())
    assert len(log.timed["ltft"]) == 2 and len(log.timed["rpm"]) == 8
    cruise = {r.name: r for r in analyze_log(log).regimes}["cruise"]
    assert cruise.n == 8 and cruise.ltft_mean == pytest.approx(3.0)
