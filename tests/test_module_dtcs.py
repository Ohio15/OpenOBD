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
    assert set(gt.gmlan) == {"243", "241"}        # EBCM/BCM via GMLAN $A9


# --------------------------------------------------------------------------- #
# CLI formatting (openobd.dtcscan._fmt) -- import-checks the CLI too
# --------------------------------------------------------------------------- #
from openobd import dtcscan  # noqa: E402


def test_cli_fmt_uds_codes():
    info = {"name": "EBCM (ABS)", "result":
            {"examined": True, "error": None, "codes": {"dtcs": ["C0035"]}}}
    assert dtcscan._fmt(info) == "C0035"


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


def a9frame(cid, b1, b2, symptom="0C", status="0A"):
    return f"{cid}81{b1}{b2}{symptom}{status}"


def test_a9_decodes_c0035_on_543():
    # EBCM report frame: 543 81 40 35 (C0035) symptom 0C status 0A
    r = parse_a9_report(a9frame("543", "40", "35"), uudt_id="543")
    assert r["examined"] is True
    assert r["codes"] == ["C0035"]
    assert r["records"] == [("C0035", "0C", "0A")]


def test_a9_end_marker_is_clean_not_silent():
    r = parse_a9_report(a9frame("543", "00", "00"), uudt_id="543")
    assert r["examined"] is True and r["codes"] == []


def test_a9_negative_response():
    r = parse_a9_report("641037FA912", uudt_id="541")
    assert r["negative"] == "12" and r["codes"] == []


def test_a9_filters_foreign_uudt_id():
    # a BCM frame (541) must not count when reading the EBCM (want 543)
    resp = a9frame("543", "40", "35") + " " + a9frame("541", "11", "22")
    r = parse_a9_report(resp, uudt_id="543")
    assert r["codes"] == ["C0035"]


def test_a9_multiple_codes():
    resp = " ".join([a9frame("543", "40", "35"), a9frame("543", "40", "40"),
                     a9frame("543", "00", "00")])
    r = parse_a9_report(resp, uudt_id="543")
    assert r["codes"] == ["C0035", "C0040"]


class GmlanFakeGt:
    def __init__(self, report, stp_ok=True, binary=False):
        self._report, self._stp_ok, self._binary = report, stp_ok, binary
        self.last_raw = b"ok"
        self.cmds = []

    def _at_checked(self, cmd, wait=0.05):
        self.cmds.append(cmd)
        return self._stp_ok if cmd.startswith("STP") else True

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
    gt = GmlanFakeGt(a9frame("543", "40", "35"))
    out = gt.read_gmlan_dtcs("243")
    assert out["examined"] is True and out["error"] is None
    assert out["codes"]["dtcs"] == ["C0035"]
    # teardown really ran: automatic protocol + CAF restored
    assert "ATSP0" in gt.cmds and "ATCAF1" in gt.cmds and "restore" in gt.cmds


def test_read_gmlan_dtcs_stp_rejected():
    gt = GmlanFakeGt("", stp_ok=False)
    out = gt.read_gmlan_dtcs("243")
    assert out["examined"] is False and "STP" in out["error"]


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
    assert set(g.gm) == {"243", "241"}           # EBCM/BCM via GMLAN $A9
