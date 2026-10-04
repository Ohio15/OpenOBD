"""SW-CAN (single-wire GMLAN) $A9 sweep over the pass-thru client
(openobd.swcan), driven by a fake client -- no driver, no hardware."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from openobd import swcan  # noqa: E402
from openobd import j2534 as jt  # noqa: E402


def frame(cid: int, hexdata: str) -> bytes:
    return cid.to_bytes(4, "big") + bytes.fromhex(hexdata)


class FakePassThru:
    """Answers writes with canned frames; records every call."""
    def __init__(self, functional=(), physical=None, fail_open=False,
                 fail_connect=False):
        self.calls = []
        self._functional = list(functional)
        self._physical = physical or {}
        self._pending: list[bytes] = []
        self._fail_open, self._fail_connect = fail_open, fail_connect

    def open(self):
        self.calls.append("open")
        if self._fail_open:
            raise RuntimeError("no device")

    def close(self):
        self.calls.append("close")

    def read_vbatt(self):
        return 12.6

    def connect(self, protocol, baud, flags=0):
        self.calls.append(("connect", protocol, baud))
        if self._fail_connect:
            raise RuntimeError("protocol not supported")
        return 7

    def disconnect(self, ch):
        self.calls.append(("disconnect", ch))

    def set_config(self, ch, params):
        self.calls.append(("set_config", tuple(params)))

    def pass_filter(self, ch, proto, pattern_id, mask_id):
        self.calls.append(("filter", proto, pattern_id, mask_id))

    def write(self, ch, tx_id, payload, proto=None, txflags=None, timeout=200):
        self.calls.append(("write", tx_id, payload.hex().upper(), proto))
        if tx_id == swcan.FUNCTIONAL_ID:
            self._pending.extend(self._functional)
        else:
            self._pending.extend(self._physical.get(tx_id, []))

    def read(self, ch, timeout=200, max_msgs=8):
        out, self._pending = self._pending, []
        return out


def run(fake):
    return swcan.sweep(fake, capture_s=0.01, per_id_s=0.0)


def test_functional_responders_decoded_with_status_filter():
    fake = FakePassThru(functional=[
        frame(0x548, "8143270011000000"),          # C0327 status 11 -> fault
        frame(0x548, "8143060001000000"),          # C0306 status 01 -> table
        frame(0x548, "8100000000000000"),          # end marker
        frame(0x551, "8100000000000000"),          # clean module
        frame(0x64D, "037FA91100000000"),          # refusal
    ])
    out = run(fake)
    assert out["examined"] is True and out["error"] is None
    assert out["responders"]["548"]["codes"] == ["C0327"]
    assert out["responders"]["548"]["table"] == ["C0327", "C0306"]
    assert out["responders"]["551"]["codes"] == []
    assert out["negatives"] == {"64D": "11"}
    assert out["vbatt"] == 12.6


def test_bus_setup_is_single_wire_pin1_33k3_with_both_filters():
    fake = FakePassThru(functional=[frame(0x551, "8100000000000000")])
    run(fake)
    assert ("connect", jt.SW_CAN_PS, 33333) in fake.calls
    assert ("set_config", ((jt.J1962_PINS, 0x0100),)) in fake.calls
    assert ("filter", jt.SW_CAN_PS, 0x500, 0x700) in fake.calls
    assert ("filter", jt.SW_CAN_PS, 0x600, 0x700) in fake.calls
    # pins set before anything is transmitted
    first_write = next(i for i, c in enumerate(fake.calls) if c[0] == "write")
    pins = fake.calls.index(("set_config", ((jt.J1962_PINS, 0x0100),)))
    assert pins < first_write


def test_only_a9_reads_are_ever_sent():
    fake = FakePassThru(functional=[frame(0x543, "8100000000000000")])
    run(fake)
    sent = [c[2] for c in fake.calls if c[0] == "write"]
    assert sent and all(p in ("FE03A981FF555555", "03A981FF55555555")
                        for p in sent)


def test_physical_pass_skips_ids_already_heard():
    fake = FakePassThru(
        functional=[frame(0x543, "8100000000000000")],        # 0x243 heard
        physical={0x251: [frame(0x551, "8140010019000000")]})
    out = run(fake)
    phys = [c[1] for c in fake.calls if c[0] == "write" and c[1] != 0x101]
    assert 0x243 not in phys and 0x251 in phys
    assert set(phys) == set(swcan.PHYSICAL_IDS) - {0x243}
    assert "551" in out["responders"]


def test_silence_is_not_examined():
    out = run(FakePassThru())
    assert out["examined"] is False and "no SW-CAN node answered" in out["error"]


def test_open_failure_reports_and_sends_nothing():
    fake = FakePassThru(fail_open=True)
    out = run(fake)
    assert out["examined"] is False and "open failed" in out["error"]
    assert not any(c[0] == "write" for c in fake.calls if isinstance(c, tuple))


def test_connect_failure_still_closes_device():
    fake = FakePassThru(fail_connect=True)
    out = run(fake)
    assert "SW-CAN sweep failed" in out["error"]
    assert fake.calls[-1] == "close"


def test_channel_disconnected_and_device_closed_after_success():
    fake = FakePassThru(functional=[frame(0x551, "8100000000000000")])
    run(fake)
    assert ("disconnect", 7) in fake.calls and fake.calls[-1] == "close"


def test_fmt_sw_sweep_prints_by_address():
    from openobd import dtcscan
    rep = {"codes": ["C0327"], "table": ["C0327"],
           "records": [("C0327", "00", "11")]}
    txt = dtcscan._fmt_sweep({"examined": True, "error": None,
                              "responders": {"548": rep}, "negatives": {}},
                             head=dtcscan._SW_HEAD, known={})
    assert txt.startswith("SW-GMLAN body bus")
    assert "0x548 UNACCOUNTED" in txt and "C0327 [11]" in txt
