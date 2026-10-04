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


def test_hs_bus_uses_plain_can_500k_without_pin_config():
    fake = FakePassThru(functional=[frame(0x543, "8140355AD3000000")])
    out = swcan.sweep(fake, bus="hs", capture_s=0.01, per_id_s=0.0)
    assert ("connect", jt.CAN, 500000) in fake.calls
    assert not any(c[0] == "set_config" for c in fake.calls
                   if isinstance(c, tuple))
    assert ("filter", jt.CAN, 0x500, 0x700) in fake.calls
    assert out["responders"]["543"]["codes"] == ["C0035"]


def test_unknown_bus_refused_before_opening():
    import pytest
    fake = FakePassThru()
    with pytest.raises(ValueError):
        swcan.sweep(fake, bus="ms")


class _BinaryGt:
    """ObdxGt stand-in whose open() reports binary mode."""
    autodetect = staticmethod(lambda: "COM3")

    def __init__(self, port):
        pass

    def open(self):
        from openobd.gt import GtBinaryMode
        raise GtBinaryMode("binary")


def test_cli_in_binary_mode_still_runs_both_sweeps(monkeypatch, capsys,
                                                    tmp_path):
    from openobd import dtcscan
    monkeypatch.chdir(tmp_path)                 # dtcscan.json lands here
    sessions = []

    def fake_sweep_buses(buses, on_result, j=None, **kw):
        sessions.append(list(buses))
        for b in buses:
            on_result(b, {"examined": False, "error": "nothing",
                          "responders": {}, "negatives": {}, "vbatt": 12.5,
                          "frames": 0})
        return None
    monkeypatch.setattr(dtcscan, "ObdxGt", _BinaryGt)
    monkeypatch.setattr(dtcscan.swcan, "sweep_buses", fake_sweep_buses)
    assert dtcscan.main() == 0
    out = capsys.readouterr().out
    assert sessions == [["hs", "sw"]]          # ONE pass-thru session
    assert "pass-thru (binary) mode" in out and "12.50 V" in out
    assert out.count("12.50 V") == 1
    import json
    saved = json.loads((tmp_path / "dtcscan.json").read_text())
    assert set(saved) == {"hs", "sw"} and saved["sw"]["vbatt"] == 12.5


def test_sweep_buses_opens_and_closes_the_device_once():
    fake = FakePassThru(functional=[frame(0x543, "8100000000000000")])
    got = []
    err = swcan.sweep_buses(["hs", "sw"], lambda b, r: got.append(b), fake,
                            capture_s=0.01, per_id_s=0.0)
    assert err is None and got == ["hs", "sw"]
    assert fake.calls.count("open") == 1 and fake.calls.count("close") == 1
    assert fake.calls[-1] == "close"
    connects = [c for c in fake.calls if isinstance(c, tuple) and c[0] == "connect"]
    assert [c[1] for c in connects] == [jt.CAN, jt.SW_CAN_PS]
    # each result delivered BEFORE the device close
    assert sum(1 for c in fake.calls if isinstance(c, tuple)
               and c[0] == "disconnect") == 2


def test_sweep_buses_delivers_results_before_close():
    order = []

    class Spy(FakePassThru):
        def close(self):
            order.append("close")
            super().close()
    fake = Spy(functional=[frame(0x543, "8100000000000000")])
    swcan.sweep_buses(["hs", "sw"], lambda b, r: order.append(b), fake,
                      capture_s=0.01, per_id_s=0.0)
    assert order == ["hs", "sw", "close"]


def test_sweep_buses_open_failure_returns_error_and_delivers_nothing():
    got = []
    err = swcan.sweep_buses(["sw"], lambda b, r: got.append(b),
                            FakePassThru(fail_open=True))
    assert "open failed" in err and got == []


def test_fmt_sweep_dedupes_faults_and_prints_table():
    from openobd import dtcscan
    rep = {"codes": ["B1000", "C0755"],
           "table": ["B1000", "C0750", "C0755", "C0327"],
           "records": [("B1000", "00", "05"), ("B1000", "00", "05"),
                       ("B1000", "00", "01"), ("C0755", "00", "01"),
                       ("C0755", "00", "13"), ("C0750", "00", "01")]}
    txt = dtcscan._fmt_sweep({"examined": True, "error": None,
                              "responders": {"558": rep}, "negatives": {}},
                             head="H", known={})
    assert "B1000 [05]" in txt and txt.count("B1000 [") == 1
    assert "C0755 [13 current]" in txt          # bit1 set
    assert "[01" not in txt                     # healthy statuses dropped
    assert "table (4): B1000 C0750 C0755 C0327" in txt


def test_fmt_sw_sweep_prints_by_address():
    from openobd import dtcscan
    rep = {"codes": ["C0327"], "table": ["C0327"],
           "records": [("C0327", "00", "11")]}
    txt = dtcscan._fmt_sweep({"examined": True, "error": None,
                              "responders": {"548": rep}, "negatives": {}},
                             head=dtcscan._SW_HEAD, known={})
    assert txt.startswith("SW-GMLAN body bus")
    assert "0x548 UNACCOUNTED" in txt and "C0327 [11]" in txt
