"""Headless tests: DTC/readiness parsing (gt.py) + failure localization
(vehnet.py). No serial hardware involved."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openobd.gt import (format_dtc, parse_dtc_response, parse_readiness,
                        parse_hs_responders)
from openobd.vehnet import (ScanResult, Status, SegStatus, localize, MODULES,
                            HS, SW)


# -- DTC decode --------------------------------------------------------------
def test_format_dtc():
    assert format_dtc(0x03, 0x00) == "P0300"
    assert format_dtc(0x01, 0x71) == "P0171"
    assert format_dtc(0x41, 0x00) == "C0100"
    assert format_dtc(0xC1, 0x21) == "U0121"
    assert format_dtc(0x11, 0x23) == "P1123"


def test_parse_dtc_response_can():
    # CAN: 43 <count> <pairs>; ELM output collapsed to hex-ish text
    assert parse_dtc_response("43 02 03 00 01 71", "03") == ["P0300", "P0171"]
    assert parse_dtc_response("4300", "03") == []
    assert parse_dtc_response("NO DATA", "03") == []
    # two ECUs answering, overlapping codes de-duped
    two = "43 01 07 00 43 01 07 00"
    assert parse_dtc_response(two, "03") == ["P0700"]
    # pending mode marker
    assert parse_dtc_response("47 01 03 01", "07") == ["P0301"]


def test_parse_readiness():
    # A=0x82 -> MIL on, 2 codes; B=0x77 supported+complete continuous;
    # C=0xFF all supported, D=0x00 all complete
    r = parse_readiness([0x82, 0x07, 0xFF, 0x00])
    assert r["mil"] is True and r["dtc_count"] == 2
    names = [n for n, _ in r["monitors"]]
    assert "Misfire" in names and "Catalyst" in names
    assert all(done for _, done in r["monitors"])
    # incomplete EVAP: D bit 2 set
    r2 = parse_readiness([0x00, 0x07, 0xFF, 0x04])
    evap = dict(r2["monitors"])["EVAP system"]
    assert evap is False
    assert parse_readiness([]) == {}


def test_parse_hs_responders():
    resp = "7E8 06 41 00 BE 3F A8 13  7EA 06 41 00 80 00 00 01"
    assert parse_hs_responders(resp) == {"7E8", "7EA"}
    assert parse_hs_responders("NO DATA") == set()


# -- pipeline localization ---------------------------------------------------
def scan(port=True, iface=True, volts=12.6, hs=None, pinged=None):
    return ScanResult(port_open=port, interface_alive=iface, dlc_volts=volts,
                      hs_responders=hs or set(), pinged=pinged or {})


def test_localize_healthy():
    v = localize(scan(hs={"7E8", "7EA"}, pinged={"ebcm": True, "bcm": True}))
    assert v.segments["pc_gt"] == SegStatus.OK
    assert v.segments["gt_dlc"] == SegStatus.OK
    assert v.segments["dlc_hs"] == SegStatus.OK
    assert v.modules["ecm"] == Status.OK
    assert v.modules["tcm"] == Status.OK
    assert v.modules["ebcm"] == Status.OK
    assert v.modules["bcm"] == Status.OK
    assert v.modules["ipc"] == Status.UNREACHABLE
    assert v.failure_point is None


def test_localize_no_interface():
    v = localize(scan(port=False, iface=False, volts=None))
    assert v.failure_point == "pc_gt"
    assert v.segments["pc_gt"] == SegStatus.FAILED
    # downstream stays unknown, not failed
    assert v.segments["dlc_hs"] == SegStatus.UNKNOWN
    assert v.modules["ecm"] == Status.UNKNOWN


def test_localize_no_dlc_power():
    v = localize(scan(volts=0.3))
    assert v.failure_point == "gt_dlc"
    assert v.segments["pc_gt"] == SegStatus.OK


def test_localize_dead_bus():
    v = localize(scan(hs=set(), pinged={"ebcm": False}))
    assert v.failure_point == "dlc_hs"
    assert v.segments["dlc_hs"] == SegStatus.FAILED
    assert v.modules["ecm"] == Status.UNKNOWN  # can't blame modules


def test_localize_single_module_down():
    v = localize(scan(hs={"7E8"}, pinged={"ebcm": True, "tcm": False}))
    assert v.segments["dlc_hs"] == SegStatus.OK
    assert v.modules["ecm"] == Status.OK
    assert v.modules["tcm"] == Status.SILENT
    assert v.failure_point == "module:tcm"


def test_module_table_shape():
    hs_mods = [m for m in MODULES if m.bus == HS]
    sw_mods = [m for m in MODULES if m.bus == SW]
    assert {m.key for m in hs_mods} == {"ecm", "tcm", "ebcm", "bcm"}
    assert len(sw_mods) >= 5
    for m in hs_mods:
        assert m.req_id and m.resp_id
