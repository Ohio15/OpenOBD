"""TCCM probe (openobd.tccmprobe): $12 failure records + $2C/$AA streaming,
driven by a fake raw-CAN pass-thru client -- no driver, no hardware."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from openobd import tccmprobe as tp  # noqa: E402


def fr(cid, hexdata):
    return cid.to_bytes(4, "big") + bytes.fromhex(hexdata)


class FakeTccm:
    """Raw-CAN stand-in for the TCCM. `script` maps a request payload (hex,
    no PCI) to a list of raw reply frames; $AA starts emit `stream` frames."""
    def __init__(self, script=None, stream=(), nak_aa04=False):
        self.script, self.stream = script or {}, list(stream)
        self.nak_aa04 = nak_aa04
        self.calls, self.q = [], []

    def open(self): self.calls.append("open")
    def close(self): self.calls.append("close")
    def connect(self, proto, baud, flags=0):
        self.calls.append(("connect", proto, baud)); return 4
    def disconnect(self, ch): self.calls.append(("disconnect", ch))
    def pass_filter(self, ch, proto, patt, mask):
        self.calls.append(("filter", patt, mask))

    def write(self, ch, tx_id, data, proto=None, txflags=None, timeout=200):
        self.calls.append(("write", tx_id, data.hex().upper()))
        if data[0] >> 4 == 3:                       # our flow control
            self.q.extend(self.script.get("FC", []))
            return
        payload = data[1:1 + (data[0] & 0x0F)].hex().upper()
        if payload.startswith("AA04") and self.nak_aa04:
            self.q.append(fr(0x7EC, "037FAA12"))
            return
        if payload.startswith("AA0") and payload != "AA00":
            self.q.extend(self.stream)
            return
        self.q.extend(self.script.get(payload, []))

    def read(self, ch, timeout=50, max_msgs=64):
        out, self.q = self.q, []
        return out


def sent_payloads(fake):
    out = []
    for c in fake.calls:
        if isinstance(c, tuple) and c[0] == "write":
            b = bytes.fromhex(c[2])
            if b[0] >> 4 == 0:
                out.append(b[1:1 + (b[0] & 0x0F)].hex().upper())
    return out


# -- allowlist --------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["AE0302020000", "04", "14FFFFFF", "2701",
                                 "1002", "28", "A503", "3400", "3B9000",
                                 "1203", "12"])
def test_non_reading_services_are_refused(bad):
    with pytest.raises(tp.NotAllowed):
        tp.check_allowed(bytes.fromhex(bad))


@pytest.mark.parametrize("ok", ["1201", "120200439800", "223114", "1A90",
                                "2CFE31143115", "AA04FEFDFCFBFA", "AA00", "3E"])
def test_reading_and_streaming_services_pass(ok):
    tp.check_allowed(bytes.fromhex(ok))


def test_raw_session_refuses_before_the_wire():
    fake = FakeTccm()
    with tp.RawSession(fake, timeout_s=0.05) as s:
        with pytest.raises(tp.NotAllowed):
            s.send(bytes.fromhex("AE0302020000"))
    assert not any(isinstance(c, tuple) and c[0] == "write" for c in fake.calls)


# -- framing / decode -------------------------------------------------------- #
def test_single_frame_padding_and_limits():
    assert tp.single_frame(b"\x12\x01") == bytes.fromhex("0212010000000000")
    with pytest.raises(ValueError):
        tp.single_frame(b"\x00" * 8)


def test_dpid_definitions_each_fit_one_frame_and_pair_is_together():
    for dpid, dids in tp.DPIDS.items():
        assert len(tp.define_dpid_request(dpid, dids)) <= 7
    assert tp.DPIDS[0xFE] == ["3114", "3115"]
    assert len(bytes([0xAA, 0x04]) + bytes(tp.DPIDS)) <= 7


def test_decode_dpid_samples_both_positions_together():
    assert tp.decode_dpid(bytes.fromhex("FE02720270000000")) == \
        {"3114": 0x272, "3115": 0x270}
    assert tp.decode_dpid(bytes.fromhex("FD05810000000000")) == \
        {"3142": 0x05, "3140": 0x81}
    assert tp.decode_dpid(bytes.fromhex("1200000000000000")) is None


def test_failure_record_parsers_on_truck_mcp_verified_shapes():
    lst = tp.parse_failure_list(bytes.fromhex("52010100403500"))
    assert lst["count"] == 1 and lst["records"][0]["dtc"] == "C0035"
    rec = tp.parse_failure_record(bytes.fromhex("520200403500"))
    assert rec["dtc"] == "C0035" and rec["parameters_hex"] == ""
    assert tp.parse_failure_list(bytes.fromhex("7F1231"))["kind"] == "negative"


# -- $12 over raw CAN, incl. multi-frame reassembly --------------------------- #
def test_failure_records_single_and_multi_frame():
    script = {
        "1201": [fr(0x7EC, "0752010100439800")],   # 52 01 01 00 43 98 00
        # record read answers multi-frame: FF then (after our FC) a CF
        "120200439800": [fr(0x7EC, "100C520200439800")],
        "FC": [fr(0x7EC, "2101020304050600")],
    }
    fake = FakeTccm(script)
    with tp.RawSession(fake, timeout_s=0.2) as s:
        res = tp.read_failure_records(s)
    assert res["error"] is None
    assert res["list"]["records"][0]["dtc"] == "C0398"
    r = res["records"][0]
    assert r["kind"] == "record" and r["dtc"] == "C0398"
    assert r["parameters_hex"] == "010203040506"   # FF 6 bytes + CF, len 0x00C
    # we sent the flow control ourselves
    assert any(isinstance(c, tuple) and c[0] == "write" and c[2].startswith("30")
               for c in fake.calls)
    assert sent_payloads(fake)[:2] == ["1201", "120200439800"]


def test_failure_records_silence_is_not_examined():
    with tp.RawSession(FakeTccm(), timeout_s=0.05) as s:
        res = tp.read_failure_records(s)
    assert res["error"] and res["records"] == []


# -- streaming --------------------------------------------------------------- #
def _define_ok():
    return {f"2C{d:02X}{''.join(ids)}": [fr(0x7EC, f"026C{d:02X}0000000000")]
            for d, ids in tp.DPIDS.items()}


def clock_ticks(*ts):
    it = iter(ts)
    last = [0.0]

    def c():
        try:
            last[0] = next(it)
        except StopIteration:
            last[0] += 10.0
        return last[0]
    return c


def test_stream_defines_starts_fast_decodes_and_always_stops():
    stream = [fr(0x5EC, "FE02420208000000"), fr(0x5EC, "FD05820000000000")]
    fake = FakeTccm(_define_ok(), stream=stream)
    got = []
    with tp.RawSession(fake, timeout_s=0.2) as s:
        res = tp.stream(s, 0.5, lambda t, v: got.append(v),
                        clock=clock_ticks(0.0, 0.1, 0.2, 0.3, 1.0))
    assert res["error"] is None and res["rate"] == "04"
    assert res["defined"] == ["FE", "FD", "FC", "FB", "FA"]
    assert {"3114": 0x242, "3115": 0x208} in got
    sp = sent_payloads(fake)
    assert sp[-1] == "AA00"                         # stream stopped
    assert all(p[:2] in ("2C", "AA", "3E") for p in sp)


def test_stream_falls_back_to_medium_rate_on_nak():
    fake = FakeTccm(_define_ok(), stream=[fr(0x5EC, "FE01770176000000")],
                    nak_aa04=True)
    with tp.RawSession(fake, timeout_s=0.2) as s:
        res = tp.stream(s, 0.1, lambda t, v: None,
                        clock=clock_ticks(0.0, 0.05, 1.0))
    assert res["rate"] == "03"
    assert "AA03FEFDFCFBFA" in sent_payloads(fake)


def test_stream_refused_definition_stops_and_reports():
    fake = FakeTccm({})                            # every $2C is silent
    with tp.RawSession(fake, timeout_s=0.05) as s:
        res = tp.stream(s, 1.0, lambda t, v: None)
    assert "refused DPID FE" in res["error"]
    assert sent_payloads(fake)[-1] == "AA00"
