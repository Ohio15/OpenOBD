"""Listen-only CAN recorder (openobd.canrec) against a fake pass-thru client."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from openobd import canrec  # noqa: E402
from openobd import j2534 as jt  # noqa: E402


def fr(cid, hexdata):
    return cid.to_bytes(4, "big") + bytes.fromhex(hexdata)


class FakeBus:
    def __init__(self, batches, fail_open=False):
        self.batches, self.calls, self.fail_open = list(batches), [], fail_open

    def open(self):
        self.calls.append("open")
        if self.fail_open:
            raise RuntimeError("no device")

    def close(self): self.calls.append("close")
    def connect(self, proto, baud, flags=0):
        self.calls.append(("connect", proto, baud)); return 9
    def disconnect(self, ch): self.calls.append(("disconnect", ch))
    def pass_filter(self, ch, proto, patt, mask):
        self.calls.append(("filter", patt, mask))

    def read(self, ch, timeout=50, max_msgs=64):
        return self.batches.pop(0) if self.batches else []

    def write(self, *a, **k):                      # must never be called
        raise AssertionError("canrec transmitted on the bus")


def clock_ticks(*ts):
    it = iter(ts)
    last = [0.0]

    def c():
        try:
            last[0] = next(it)
        except StopIteration:
            last[0] += 1.0
        return last[0]
    return c


def test_records_tool_request_and_module_reply_without_transmitting():
    bus = FakeBus([[fr(0x7E4, "02AE01"), fr(0x7EC, "02EE01")], []])
    got = []
    res = canrec.record(bus, 1.0, got.append,
                        clock=clock_ticks(0.0, 0.1, 0.2, 0.3, 2.0))
    assert res == {"frames": 2, "error": None}
    assert [(g["id"], g["data"]) for g in got] == [("7E4", "02AE01"),
                                                   ("7EC", "02EE01")]
    assert ("connect", jt.CAN, 500000) in bus.calls
    assert bus.calls[-1] == "close" and ("disconnect", 9) in bus.calls


def test_filters_cover_the_tccm_and_functional_ids_within_the_limit():
    bus = FakeBus([])
    canrec.record(bus, 0.0, lambda r: None, clock=clock_ticks(0.0, 1.0))
    filt = [(c[1], c[2]) for c in bus.calls if isinstance(c, tuple)
            and c[0] == "filter"]
    assert len(filt) <= 10
    def passes(cid):
        return any(cid & m == p & m for p, m in filt)
    for cid in (0x7E4, 0x7EC, 0x5EC, 0x7DF, 0x101, 0x243, 0x643, 0x543):
        assert passes(cid), hex(cid)
    assert not passes(0x0C9)                       # ordinary bus traffic


def test_open_failure_reports_and_records_nothing():
    res = canrec.record(FakeBus([], fail_open=True), 1.0, lambda r: None)
    assert res["frames"] == 0 and "open failed" in res["error"]


def test_ctrl_c_is_a_clean_stop_and_closes():
    class Interrupting(FakeBus):
        def read(self, ch, timeout=50, max_msgs=64):
            raise KeyboardInterrupt
    bus = Interrupting([])
    res = canrec.record(bus, 10.0, lambda r: None)
    assert res["error"] is None and bus.calls[-1] == "close"


class FakeSwBus(FakeBus):
    def __init__(self, batches):
        super().__init__(batches)

    def set_config(self, ch, params):
        self.calls.append(("config", tuple(params)))


def test_sw_bus_uses_single_wire_pin1_and_tags_records():
    bus = FakeSwBus([[fr(0x24D, "02AE05"), fr(0x64D, "02EE05")], []])
    got = []
    res = canrec.record(bus, 1.0, got.append, bus="sw",
                        clock=clock_ticks(0.0, 0.1, 0.2, 0.3, 2.0))
    assert res["frames"] == 2
    assert ("connect", jt.SW_CAN_PS, jt.SW_CAN_BAUD) in bus.calls
    assert ("config", ((jt.J1962_PINS, jt.SW_CAN_PINS),)) in bus.calls
    assert {g["bus"] for g in got} == {"sw"}
    filt = [(c[1], c[2]) for c in bus.calls if isinstance(c, tuple)
            and c[0] == "filter"]
    for cid in (0x24D, 0x25D, 0x54D, 0x64D, 0x101):
        assert any(cid & m == p & m for p, m in filt), hex(cid)


def test_hs_records_are_tagged_hs():
    bus = FakeBus([[fr(0x7E4, "02AE03")], []])
    got = []
    canrec.record(bus, 1.0, got.append, clock=clock_ticks(0.0, 0.1, 0.2, 2.0))
    assert got[0]["bus"] == "hs"


def test_unknown_bus_refused_before_opening():
    import pytest
    bus = FakeBus([])
    with pytest.raises(ValueError):
        canrec.record(bus, 1.0, lambda r: None, bus="ms")
    assert "open" not in bus.calls


def test_default_label_names_the_bus_and_time():
    import time as _t
    t = _t.mktime((2026, 10, 5, 18, 22, 33, 0, 0, -1))
    assert canrec.default_label("sw", t) == "body-20261005-182233"
    assert canrec.default_label("hs", t) == "main-20261005-182233"


def test_cli_without_label_uses_the_default(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    import openobd.j2534 as jtm
    monkeypatch.setattr(jtm, "J2534", lambda *a, **k: FakeSwBus([]))
    monkeypatch.setattr(canrec, "default_label", lambda bus, now=None: f"{bus}-X")
    monkeypatch.setattr(canrec, "record",
                        lambda j, s, cb, bus="hs", **k: {"frames": 0, "error": None})
    assert canrec.main(["--bus", "sw"]) == 0
    assert (tmp_path / "canrec-sw-X.jsonl").exists()


def test_module_has_no_transmit_path():
    src = pathlib.Path(canrec.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith(("#", '"', "'")))
    assert ".write(" not in code.replace("fh.write(", "")
