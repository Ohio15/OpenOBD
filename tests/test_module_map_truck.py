"""Regression tests for the Module Map defects found on Ron's truck
(2010 Silverado 1500, E38 / T43, OBDX Pro GT) on 2026-09-26.

Every scenario replays the exact adapter behaviour observed on the truck
through a fake serial port — no hardware is opened. The fake models the
parts of the GT that the defects depended on: the tx header actually in
force, the CAN receive filter, the '?' answer to a refused AT command, the
single-line reply to the functional 0100 broadcast, and the binary J2534-mode
reply to text commands.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd import gt as gtmod
from openobd.gt import (ObdxGt, is_binary_reply, parse_atrv,
                        parse_hs_responders, reply_has_frame_from)
from openobd.vehnet import (MODULES, HS, SW, ScanResult, SegStatus, Status,
                            localize, scan_pipeline)

# Exact bytes the GT sent back to ATI after a J2534 session (2026-09-26).
BINARY_REPLY = b"\x7f\x02A\x01<"

# Physical request -> response id on THIS truck (the thing under test).
TRUCK_ROUTES = {"7E0": "7E8", "7E2": "7EA", "243": "643", "241": "641"}


class FakeTruckGt:
    """Serial-port double for the OBDX GT on the truck.

    refuse: {command: n} — answer '?' to the next n sends of that command.
    alive:  set of response ids whose module answers TesterPresent.
    """

    def __init__(self, *, binary=False, atrv=b"12.6V", refuse=None,
                 alive=None, ati=b"ELM327 v2.1", at1=b"OBDX Pro GT"):
        self.binary = binary
        self.atrv = atrv
        self.refuse = dict(refuse or {})
        self.alive = set(TRUCK_ROUTES.values()) if alive is None else alive
        self.ati = ati
        self.at1 = at1
        self.header = "7DF"          # ELM power-on default
        self.filter = None           # None = automatic (7E8..7EF only)
        self.headers_on = False
        self.log = []                # (command, header at send time)
        self._out = b""

    # -- pyserial surface ---------------------------------------------------
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
        self.log.append((cmd, self.header))
        self._out = self._answer(cmd)

    # -- adapter model ------------------------------------------------------
    def _answer(self, cmd):
        if self.binary:
            return BINARY_REPLY          # no '>' prompt, ever
        if self.refuse.get(cmd, 0) > 0:
            self.refuse[cmd] -= 1
            return b"?\r\r>"
        if cmd == "ATI":
            return self.ati + b"\r\r>"
        if cmd == "AT@1":
            return self.at1 + b"\r\r>"
        if cmd == "ATRV":
            return self.atrv + b"\r\r>"
        if cmd.startswith("ATSH"):
            self.header = cmd[4:]
            return b"OK\r\r>"
        if cmd == "ATCRA":
            self.filter = None
            return b"OK\r\r>"
        if cmd.startswith("ATCRA"):
            self.filter = cmd[5:]
            return b"OK\r\r>"
        if cmd in ("ATH1", "ATH0"):
            self.headers_on = cmd == "ATH1"
            return b"OK\r\r>"
        if cmd.startswith("AT"):
            return b"OK\r\r>"
        if cmd == "0100":
            # The GT's ELM layer returned ONE responder line for the
            # functional broadcast on the truck, although ECM, TCM and more
            # answer on the bus. A physical 7E0 request likewise gets only 7E8.
            if self.header in ("7DF", "7E0") and "7E8" in self.alive:
                return self._frame("7E8", "064100BE3FA813")
            return b"NO DATA\r\r>"
        if cmd == "3E":
            resp = TRUCK_ROUTES.get(self.header)
            if resp and resp in self.alive and self._passes(resp):
                return self._frame(resp, "017E")
            return b"NO DATA\r\r>"
        return b"NO DATA\r\r>"

    def _passes(self, can_id):
        if self.filter is None:           # automatic filter: 7E8..7EF
            return can_id.startswith("7E") and can_id[2] in "89ABCDEF"
        return can_id == self.filter

    def _frame(self, can_id, payload):
        line = (can_id + payload) if self.headers_on else payload
        return line.encode() + b"\r\r>"

    def sent(self):
        return [c for c, _ in self.log]


@pytest.fixture
def make_gt(monkeypatch):
    monkeypatch.setattr(gtmod.time, "sleep", lambda s: None)

    def factory(**kw):
        g = ObdxGt(port="FAKE")
        g.ser = FakeTruckGt(**kw)
        g.read_deadline_s = 0.02
        return g
    return factory


# -- defect 1: addresses ------------------------------------------------------
def test_module_addresses_match_the_truck():
    table = {m.key: (m.bus, m.req_id, m.resp_id) for m in MODULES}
    assert table["ecm"] == (HS, "7E0", "7E8")
    assert table["tcm"] == (HS, "7E2", "7EA")
    assert table["ebcm"] == (HS, "243", "643")
    assert table["bcm"] == (HS, "241", "641")
    ebcm = next(m for m in MODULES if m.key == "ebcm")
    assert ebcm.obd_responder is False
    # no invented modules for the unidentified 7EB / 7EC / 64D responders
    assert not {"7EB", "7EC", "64D"} & {m.resp_id for m in MODULES}
    for m in MODULES:
        if m.bus == SW:
            assert m.req_id is None and m.resp_id is None


# -- defect 2/3: binary J2534 mode ------------------------------------------
def test_binary_reply_signature():
    assert is_binary_reply(BINARY_REPLY)
    assert not is_binary_reply(b"ELM327 v2.1\r\r>")
    assert not is_binary_reply(b"12.6V\r\r>")
    assert is_binary_reply(b"\x80OK")


def test_binary_mode_is_not_interface_alive(make_gt):
    g = make_gt(binary=True)
    facts = g.scan_network()
    assert facts["interface_alive"] is False
    assert facts["binary_mode"] is True
    assert "\\x7f" in facts["interface_reply"]


def test_binary_mode_verdict_not_dlc(make_gt):
    g = make_gt(binary=True)
    result = scan_pipeline(g)
    assert result.pinged == {}                      # no pings into garbage
    assert "3E" not in g.ser.sent()
    v = localize(result)
    assert v.failure_point == "pc_gt:binary_mode"
    assert v.segments["pc_gt"] == SegStatus.FAILED
    assert v.segments["gt_dlc"] == SegStatus.UNKNOWN
    text = " ".join(v.notes)
    assert "binary (J2534) mode" in text
    assert "Unplug the GT from USB AND the OBD port for 10 s" in text
    assert "voltage" not in text.lower()
    assert all(v.modules[m.key] != Status.SILENT for m in MODULES)


def test_unidentified_text_reply_is_not_alive(make_gt):
    g = make_gt(ati=b"?", at1=b"?")
    facts = g.scan_network()
    assert facts["interface_alive"] is False and facts["binary_mode"] is False
    v = localize(scan_pipeline(make_gt(ati=b"?", at1=b"?")))
    assert v.failure_point == "pc_gt"


# -- defect 3: unparseable ATRV ---------------------------------------------
@pytest.mark.parametrize("reply", [b"?", b"12.6", b"V", b"ATRV", b""])
def test_unparseable_atrv_is_unreadable_not_no_voltage(make_gt, reply):
    g = make_gt(atrv=reply)
    result = scan_pipeline(g)
    assert result.dlc_volts is None
    v = localize(result)
    assert v.failure_point != "gt_dlc"
    assert v.segments["gt_dlc"] == SegStatus.UNKNOWN
    text = " ".join(v.notes)
    assert "unreadable" in text
    assert "pin 16" not in text and "Low battery" not in text
    # the scan carried on and the modules were really checked
    assert v.segments["dlc_hs"] == SegStatus.OK
    assert v.modules["ecm"] == Status.OK


def test_parsed_low_voltage_still_fails_dlc(make_gt):
    v = localize(scan_pipeline(make_gt(atrv=b"0.2V")))
    assert v.failure_point == "gt_dlc"
    assert v.segments["gt_dlc"] == SegStatus.FAILED


def test_parse_atrv():
    assert parse_atrv("12.6V") == 12.6
    assert parse_atrv(" 14.2 V ") == 14.2
    assert parse_atrv("?") is None
    assert parse_atrv("12.6") is None
    assert parse_atrv("") is None


# -- defect 4: '?' on ATSH ---------------------------------------------------
def test_refused_header_marks_module_not_examined(make_gt):
    # the EBCM header is refused on both the attempt and the retry
    g = make_gt(refuse={"ATSH243": 2})
    result = scan_pipeline(g)
    assert result.pinged["ebcm"] is None
    v = localize(result)
    assert v.modules["ebcm"] == Status.NOT_EXAMINED
    assert v.modules["ebcm"] != Status.SILENT
    assert v.failure_point is None
    # no TesterPresent went out on a stale header for the EBCM
    ebcm_sends = [h for c, h in g.ser.log if c == "3E"]
    assert "7E0" not in ebcm_sends and "243" not in ebcm_sends
    # the others were still checked and answered
    assert v.modules["tcm"] == Status.OK
    assert v.modules["bcm"] == Status.OK


def test_intermittent_question_mark_is_retried(make_gt):
    # the truck transcript: ATH1 -> OK, then ATSH7E2 -> '?' once
    g = make_gt(refuse={"ATSH7E2": 1})
    assert g.ping_module("7E2", "7EA") is True
    assert g.ser.sent().count("ATSH7E2") == 2
    assert ("3E", "7E2") in g.ser.log


def test_refused_filter_marks_not_examined(make_gt):
    g = make_gt(refuse={"ATCRA641": 2})
    assert g.ping_module("241", "641") is None


def test_failed_header_restore_is_tracked(make_gt):
    g = make_gt(refuse={"ATSH7E0": 2})
    assert g.ping_module("7E2", "7EA") is True
    assert g._header is None                     # restore refused, known-unknown
    # a later ECM DID read must not go out on the leftover 7E2 header
    g.ser.refuse["ATSH7E0"] = 2
    assert g.request_did("1644") is None
    assert "221644" not in g.ser.sent()


def test_request_did_checks_module_header(make_gt):
    g = make_gt(refuse={"ATSH7E2": 2})
    g._header = "7E0"
    assert g.request_did("1940", module="7E2") is None
    assert "221940" not in g.ser.sent()


# -- defect 5: broadcast absence ---------------------------------------------
def test_single_line_broadcast_with_physical_pings_all_ok(make_gt):
    g = make_gt()
    # state after open(): physical ECM header in force for DID polling
    g.ser.header, g._header = "7E0", "7E0"
    result = scan_pipeline(g)
    assert result.hs_responders == {"7E8"}       # only ONE line came back
    # the broadcast really went out functionally (root cause: it used to be
    # sent on the leftover 7E0 physical header)
    assert ("0100", "7DF") in g.ser.log
    v = localize(result)
    for key in ("ecm", "tcm", "ebcm", "bcm"):
        assert v.modules[key] == Status.OK, key
    assert v.failure_point is None
    # pings went to the truck's real addresses
    pinged_on = {h for c, h in g.ser.log if c == "3E"}
    assert pinged_on == {"7E2", "243", "241"}
    assert g._header == "7E0"                    # left on the ECM for polling


def test_verified_silent_module_is_silent(make_gt):
    alive = set(TRUCK_ROUTES.values()) - {"643"}
    v = localize(scan_pipeline(make_gt(alive=alive)))
    assert v.modules["ebcm"] == Status.SILENT
    assert v.failure_point == "module:ebcm"


def test_broadcast_absence_alone_is_not_silence():
    v = localize(ScanResult(port_open=True, interface_alive=True,
                            dlc_volts=12.6, hs_responders={"7E8"}, pinged={}))
    assert v.modules["ecm"] == Status.OK
    assert v.modules["tcm"] == Status.UNKNOWN    # was SILENT via obd_responder
    assert v.failure_point is None


def test_all_headers_refused_is_not_a_bus_fault():
    v = localize(ScanResult(port_open=True, interface_alive=True,
                            dlc_volts=12.6, hs_responders=set(),
                            pinged={"ecm": None, "tcm": None, "ebcm": None,
                                    "bcm": None}))
    assert v.segments["dlc_hs"] == SegStatus.UNKNOWN
    assert v.failure_point is None
    assert all(v.modules[k] == Status.NOT_EXAMINED
               for k in ("ecm", "tcm", "ebcm", "bcm"))


def test_multi_line_broadcast_parsed():
    unspaced = "7E8064100BE3FA813 7EA064100800000017EB06410080000001"
    assert parse_hs_responders(unspaced) == {"7E8", "7EA", "7EB"}
    spaced = "SEARCHING... 7E8 06 41 00 BE 3F A8 13  7EA 06 41 00 80 00 00 01"
    assert parse_hs_responders(spaced) == {"7E8", "7EA"}


def test_frame_match_ignores_data_bytes():
    assert reply_has_frame_from("641017E", "641")
    assert reply_has_frame_from("641 01 7E", "641")
    assert not reply_has_frame_from("7E8064300", "643")
    assert not reply_has_frame_from("7E8 06 43 00", "643")
    assert not reply_has_frame_from("NO DATA", "641")
