"""Per-module DTC read (gt.parse_uds19_reply, gt.read_module_dtcs,
vehnet.scan_all_module_dtcs).

Reaches the EBCM/BCM that the functional read_dtcs() cannot. All synthetic —
no hardware. The UDS $19 fixtures use the real DTC byte encoding (C0035 = 40 35).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd.gt import parse_uds19_reply, format_dtc  # noqa: E402
from openobd import vehnet  # noqa: E402


# --------------------------------------------------------------------------- #
# UDS $19 02 parser
# --------------------------------------------------------------------------- #
# frame builder: <3hex id><SF PCI = 0x0 + len><payload hex>, one CAN frame.
def sf(cid, payload_hex):
    n = len(payload_hex) // 2
    return f"{cid}0{n:X}{payload_hex}"


def test_uds19_decodes_c0035():
    # 59 02 FF | DTC 40 35 00 (C0035, ftb 00) status 09
    resp = sf("643", "5902FF40350009")
    r = parse_uds19_reply(resp, "643")
    assert r["examined"] is True
    assert r["codes"] == ["C0035"]
    assert r["records"] == [("C0035", 0x00, 0x09)]


def test_uds19_multiple_codes():
    resp = sf("643", "5902FF" + "40350009" + "C0A00008" + "01180008")
    r = parse_uds19_reply(resp, "643")
    # C0035, U1... , P0118 — format from the first two bytes
    assert "C0035" in r["codes"]
    assert format_dtc(0xC0, 0xA0) in r["codes"]
    assert format_dtc(0x01, 0x18) in r["codes"]
    assert len(r["codes"]) == 3


def test_uds19_test_not_completed_only_is_not_a_fault():
    # status 0x50 = bits 4 and 6 only (test not completed) -> table entry
    resp = sf("643", "5902FF" + "40350050" + "40450009")
    r = parse_uds19_reply(resp, "643")
    assert r["codes"] == ["C0045"]
    assert len(r["records"]) == 2


def test_uds19_no_dtc_filler_skipped():
    resp = sf("643", "5902FF00000000")      # all-zero DTC = 'no codes' filler
    r = parse_uds19_reply(resp, "643")
    assert r["examined"] is True            # the module DID answer
    assert r["codes"] == []                 # but holds no codes


def test_uds19_filters_by_responder():
    # a stray frame from another id must not be attributed to 643
    resp = sf("643", "5902FF40350009") + " " + sf("7EA", "5902FF11223300")
    r = parse_uds19_reply(resp, "643")
    assert r["codes"] == ["C0035"]


def test_uds19_non_5902_reply_not_examined():
    resp = sf("643", "7F1911")              # negative response to $19
    r = parse_uds19_reply(resp, "643")
    assert r["examined"] is False
    assert r["codes"] == []


# --------------------------------------------------------------------------- #
# read_module_dtcs (physical, via a duck-typed fake GT)
# --------------------------------------------------------------------------- #
class FakeGt:
    """Minimal ObdxGt surface read_module_dtcs touches."""
    def __init__(self, replies, binary=False):
        self._replies = replies          # cmd -> reply text
        self.last_raw = b"" if binary else b"7E8 OK"
        self._binary = binary
        self.calls = []

    # read_module_dtcs uses these:
    def _prepare(self, header, headers_on):
        self._header = header
        return None                      # header set ok

    def _restore_default(self):
        self._header = "7E0"

    def command(self, cmd, wait=0.0):
        self.calls.append(cmd)
        reply = self._replies.get(cmd, "?")
        self.last_raw = b"\x7f\x02" if (self._binary and reply == "BIN") else b"ok"
        return "" if reply == "BIN" else reply


# bind the real method onto the fake (it only uses the surface above)
from openobd.gt import ObdxGt  # noqa: E402
FakeGt.read_module_dtcs = ObdxGt.read_module_dtcs


def test_read_module_obd_powertrain():
    # TCM 7E2/7EA, mode 03 returns one DTC from 7EA
    gt = FakeGt({"03": sf("7EA", "43010010"), "07": "?", "0A": "?"})
    out = gt.read_module_dtcs("7E2", "7EA", uds=False)
    assert out["examined"] is True and out["error"] is None
    assert out["codes"]["stored"] == ["P0010"]
    # 07/0A returned '?' (no usable answer) -> unexamined, key absent (not "no codes")
    assert "pending" not in out["codes"] and "permanent" not in out["codes"]


def test_read_module_uds_ebcm():
    gt = FakeGt({"1902FF": sf("643", "5902FF40350009")})
    out = gt.read_module_dtcs("243", "643", uds=True)
    assert out["examined"] is True and out["error"] is None
    assert out["codes"]["dtcs"] == ["C0035"]


def test_read_module_uds_nak_reports_legacy_hint():
    gt = FakeGt({"1902FF": sf("643", "7F1911")})   # module NAKs $19
    out = gt.read_module_dtcs("243", "643", uds=True)
    assert out["examined"] is False
    assert "legacy" in out["error"]


def test_read_module_silence_is_not_no_codes():
    gt = FakeGt({})                                 # module answers nothing
    out = gt.read_module_dtcs("7E2", "7EA", uds=False)
    assert out["examined"] is False
    assert out["error"] and "did not answer" in out["error"]


# --------------------------------------------------------------------------- #
# scan_all_module_dtcs orchestration
# --------------------------------------------------------------------------- #
class RecordingGt:
    def __init__(self):
        self.obd = []
        self.gmlan = []

    def read_module_dtcs(self, req_id, resp_id, uds=False):
        self.obd.append(req_id)
        return {"examined": True, "error": None, "codes": {}, "raw": {}}

    def read_gmlan_dtcs(self, req_id, sw=False):
        self.gmlan.append(req_id)
        return {"examined": True, "error": None, "codes": {"dtcs": []}, "raw": ""}


def test_scan_all_reads_hs_and_marks_sw_unreachable():
    res = vehnet.scan_all_module_dtcs(RecordingGt())
    # HS modules with ids are read; SW modules are unreachable
    assert "result" in res["ecm"] and "result" in res["tcm"]
    assert "result" in res["ebcm"] and "result" in res["bcm"]
    assert "unreachable" in res["ipc"] and "unreachable" in res["tccm"]


def test_scan_all_routes_powertrain_to_obd_gmlan_to_a9():
    gt = RecordingGt()
    vehnet.scan_all_module_dtcs(gt)
    assert set(gt.obd) == {"7E0", "7E2"}          # ECM/TCM via OBD modes
    # EBCM/BCM, then the unidentified HS-GMLAN ids, via GMLAN $A9
    assert set(gt.gmlan) == {"243", "241", "242", "24D"}


def test_scan_all_reads_unidentified_ids_by_address_only():
    res = vehnet.scan_all_module_dtcs(RecordingGt())
    assert res["hs242"]["name"] == "HS 0x242 (unidentified)"
    assert res["hs24d"]["name"] == "HS 0x24D (unidentified)"
    assert "result" in res["hs242"]
    # never promoted into MODULES (the map names only identified modules)
    assert not {"242", "24D"} & {m.req_id for m in vehnet.MODULES}


# --------------------------------------------------------------------------- #
# CLI formatting (openobd.dtcscan._fmt) -- import-checks the CLI too
# --------------------------------------------------------------------------- #
from openobd import dtcscan  # noqa: E402


def test_cli_fmt_uds_codes():
    info = {"name": "EBCM (ABS)", "result":
            {"examined": True, "error": None, "codes": {"dtcs": ["C0035"]}}}
    assert dtcscan._fmt(info) == "C0035"


def test_cli_fmt_a9_shows_status_and_hides_healthy_table():
    info = {"name": "EBCM (ABS)", "result": {
        "examined": True, "error": None,
        "codes": {"dtcs": ["C0035"], "table": ["C0550", "C0035", "C0045"]},
        "records": [("C0550", "00", "01"), ("C0035", "5A", "D3"),
                    ("C0045", "00", "01")]}}
    out = dtcscan._fmt(info)
    assert out.startswith("C0035 [D3 current]")
    assert "C0550" not in out and "C0045" not in out
    assert "3 supported-DTC table entries read; 2 healthy" in out


def test_cli_fmt_a9_clean_table_says_no_fault_codes():
    info = {"name": "BCM", "result": {
        "examined": True, "error": None,
        "codes": {"dtcs": [], "table": ["B0005", "B1000"]},
        "records": [("B0005", "00", "01"), ("B1000", "00", "19")]}}
    out = dtcscan._fmt(info)
    assert out.startswith("no fault codes") and "2 healthy" in out


def test_cli_fmt_obd_and_unreachable_and_unexamined():
    obd = {"name": "ECM", "result": {"examined": True, "error": None,
           "codes": {"stored": ["P0010"], "pending": []}}}
    assert "stored: P0010" in dtcscan._fmt(obd) and "pending: none" in dtcscan._fmt(obd)
    assert dtcscan._fmt({"name": "IPC", "unreachable": "no HS id"}) == "-- no HS id"
    une = {"name": "EBCM", "result": {"examined": False, "error": "no 59 02 reply",
           "codes": {}}}
    assert "NOT EXAMINED" in dtcscan._fmt(une) and "59 02" in dtcscan._fmt(une)


# --------------------------------------------------------------------------- #
# GMLAN $A9 read (EBCM/BCM) -- the verified chassis/body path (not UDS $19)
# --------------------------------------------------------------------------- #
from openobd.gt import parse_a9_report  # noqa: E402


# Real GT $A9 report format (verified on the EBCM 2026-10-04): NO CAN id prefix,
# '81 <hi> <lo> <symptom> <status>' padded to 8 bytes. 8140355A01... = C0035.
def a9(b1, b2, symptom="5A", status="01"):
    return f"81{b1}{b2}{symptom}{status}000000"


def a9_id(cid, b1, b2):           # the id-prefixed variant (also supported)
    return f"{cid}81{b1}{b2}0C0A"


# a verbatim slice of the real EBCM capture
REAL_EBCM = ("8145500001000000 8148990001000000 8149000001000000 "
             "8140355A01000000 8140350001000000 8140455A01000000 "
             "8140450001000000 8140405A01000000 8140400001000000")


def test_a9_real_ebcm_capture_is_table_not_faults():
    # The 2026-10-04 capture: every entry carries housekeeping status 01, so it
    # is the module's supported-DTC TABLE and holds no fault. This test used to
    # assert C0035 was a fault here — that was the defect.
    r = parse_a9_report(REAL_EBCM, uudt_id="543")
    assert r["examined"] is True
    assert r["codes"] == []
    assert r["table"] == ["C0550", "C0899", "C0900", "C0035", "C0045", "C0040"]
    assert r["table"].count("C0035") == 1     # deduped (symptom 5A and 00)


def test_a9_live_c0035_status_d3_is_a_fault():
    # D3 is the status truck-mcp read on the live LF wheel-speed fault.
    r = parse_a9_report(a9("40", "35", status="D3") + " " + a9("45", "50"))
    assert r["codes"] == ["C0035"]
    assert r["table"] == ["C0035", "C0550"]


def test_a9_status_rule():
    from openobd.gt import a9_status_is_fault
    for healthy in ("01", "19", "21", "25"):
        assert a9_status_is_fault(healthy) is False
    assert a9_status_is_fault("03") is True    # bit1 = currently failed
    assert a9_status_is_fault("D3") is True
    assert a9_status_is_fault("40") is True    # unknown status is never clean
    assert a9_status_is_fault("zz") is True    # unparseable is never clean


def test_a9_fault_if_any_record_for_code_is_faulty():
    # same DTC, two symptoms: the healthy entry first must not mask the fault
    r = parse_a9_report(a9("40", "35", "00", "01") + " " + a9("40", "35", "5A", "D3"))
    assert r["codes"] == ["C0035"]
    assert r["records"] == [("C0035", "00", "01"), ("C0035", "5A", "D3")]


def test_a9_decodes_c0035_noid():
    r = parse_a9_report(a9("40", "35", status="D3"))
    assert r["codes"] == ["C0035"] and r["records"] == [("C0035", "5A", "D3")]


def test_a9_id_prefixed_still_supported():
    r = parse_a9_report(a9_id("543", "40", "35"), uudt_id="543")
    assert r["codes"] == ["C0035"]


def test_a9_end_marker_is_clean_not_silent():
    r = parse_a9_report(a9("00", "00"))
    assert r["examined"] is True and r["codes"] == []


def test_a9_negative_response():
    assert parse_a9_report("7FA912")["negative"] == "12"       # no-id form
    assert parse_a9_report("641037FA912")["negative"] == "12"  # id-prefixed form


def test_a9_dedups_repeated_dtc():
    r = parse_a9_report(a9("40", "35", "5A", "D3") + " " + a9("40", "35", "00", "D3")
                        + " " + a9("40", "35", "5A", "D3"))
    assert r["codes"] == ["C0035"]
    assert len(r["records"]) == 2             # identical frame counted once


class GmlanFakeGt:
    def __init__(self, report, atsp_ok=True, binary=False):
        self._report, self._atsp_ok, self._binary = report, atsp_ok, binary
        self.last_raw = b"ok"
        self.cmds = []

    def _at_checked(self, cmd, wait=0.05):
        self.cmds.append(cmd)
        return self._atsp_ok if cmd.startswith("ATSP") else True

    def _set_header(self, h):
        self.cmds.append("SH:" + h)
        return True

    def _restore_default(self):
        self.cmds.append("restore")

    def command(self, cmd, wait=0.0, deadline=None):
        self.cmds.append(cmd)
        if cmd.startswith("03A981"):
            self.last_raw = b"" if self._binary else b"ok"
            return "" if self._binary else self._report
        return "OK"


GmlanFakeGt.read_gmlan_dtcs = ObdxGt.read_gmlan_dtcs


def test_read_gmlan_dtcs_reads_c0035():
    gt = GmlanFakeGt(a9("40", "35", status="D3") + " " + a9("45", "50"))
    out = gt.read_gmlan_dtcs("243")
    assert out["examined"] is True and out["error"] is None
    assert out["codes"]["dtcs"] == ["C0035"]
    assert out["codes"]["table"] == ["C0035", "C0550"]
    # addressed the EBCM's report id, and the teardown really ran
    assert "ATCRA543" in gt.cmds
    assert "ATSP0" in gt.cmds and "ATCAF1" in gt.cmds and "restore" in gt.cmds


def test_read_gmlan_dtcs_atsp_rejected():
    gt = GmlanFakeGt("", atsp_ok=False)
    out = gt.read_gmlan_dtcs("243")
    assert out["examined"] is False and "ATSP6" in out["error"]


def test_read_gmlan_dtcs_negative():
    gt = GmlanFakeGt("643037FA922")
    out = gt.read_gmlan_dtcs("243")
    assert out["examined"] is False and "NRC 22" in out["error"]


def test_scan_all_routes_gmlan_to_a9():
    class G:
        def __init__(self): self.obd = []; self.gm = []
        def read_module_dtcs(self, req, resp, uds=False):
            self.obd.append(req); return {"examined": True, "codes": {}, "error": None}
        def read_gmlan_dtcs(self, req, sw=False):
            self.gm.append(req); return {"examined": True, "codes": {"dtcs": []}, "error": None}
    g = G()
    vehnet.scan_all_module_dtcs(g)
    assert set(g.obd) == {"7E0", "7E2"}          # ECM/TCM via OBD
    assert set(g.gm) == {"243", "241", "242", "24D"}   # all via GMLAN $A9


# --------------------------------------------------------------------------- #
# functional $A9 sweep (gt.sweep_gmlan_dtcs + dtcscan._fmt_sweep)
# --------------------------------------------------------------------------- #
class SweepFakeGt(GmlanFakeGt):
    """Answers the functional $A9 with headers-on UUDT/USDT frames."""
    def __init__(self, uudt, usdt, refuse=()):
        super().__init__("")
        self._uudt, self._usdt, self._refuse = uudt, usdt, refuse
        self._filt = None

    def command(self, cmd, wait=0.0, deadline=None):
        self.cmds.append(cmd)
        if cmd in self._refuse:
            return "?"
        if cmd.startswith("ATCF"):
            self._filt = cmd[4:]
        if cmd == "FE03A981FF555555":
            return self._uudt if self._filt == "500" else self._usdt
        return "OK"

    def _at_checked(self, cmd, wait=0.05):
        return self.command(cmd) != "?"

    def _set_header(self, h):
        return self.command("ATSH" + h) != "?"


def test_sweep_lists_every_responder_and_negative():
    from openobd.gt import ObdxGt
    uudt = ("54181A645077F000000 54181000000000000 "
            "5438140355AD3000000 5438145500001000000 "
            "5488143270011000000")
    gt = SweepFakeGt(uudt, "64D037FA911000000")
    out = ObdxGt.sweep_gmlan_dtcs(gt)
    assert out["examined"] is True and out["error"] is None
    assert set(out["responders"]) == {"541", "543", "548"}
    assert out["responders"]["543"]["codes"] == ["C0035"]
    assert out["responders"]["548"]["codes"] == ["C0327"]
    assert out["negatives"] == {"64D": "11"}
    assert "ATSH101" in gt.cmds and "FE03A981FF555555" in gt.cmds
    assert gt.cmds[-1] != "FE03A981FF555555"     # state restored after


def test_sweep_refused_filter_sends_nothing():
    from openobd.gt import ObdxGt
    gt = SweepFakeGt("", "", refuse=("ATCF500",))
    out = ObdxGt.sweep_gmlan_dtcs(gt)
    assert out["examined"] is False and "0x500" in out["error"]
    assert "FE03A981FF555555" not in gt.cmds


def test_sweep_silence_is_not_examined():
    from openobd.gt import ObdxGt
    out = ObdxGt.sweep_gmlan_dtcs(SweepFakeGt("NO DATA", "NO DATA"))
    assert out["examined"] is False and "raw kept" in out["error"]


def test_fmt_sweep_flags_unaccounted():
    rep = {"codes": ["C0327"], "table": ["C0327", "C0306"],
           "records": [("C0327", "00", "11"), ("C0306", "00", "01")]}
    txt = dtcscan._fmt_sweep({"examined": True, "error": None,
                              "responders": {"548": rep}, "negatives": {"64D": "11"}})
    assert "0x548 UNACCOUNTED" in txt and "C0327 [11]" in txt
    assert "0x64D refused $A9 (NRC 11)" in txt
