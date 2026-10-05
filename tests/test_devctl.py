"""Gated device control (openobd.devctl) against a fake raw-CAN truck."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from openobd import devctl as dc  # noqa: E402

ENTRY = {"id": "7E4-AE03-1", "module": "7E4", "request": "AE0302020000",
         "cpid": "03", "name": "test", "status": "captured", "max_seconds": 5}


def fr(cid, hexdata):
    return cid.to_bytes(4, "big") + bytes.fromhex(hexdata)


class FakeTruck:
    """ECM answers rpm/speed; TCCM answers $2C, streams on $AA, and answers
    the device control with `devctl_reply` frames."""
    def __init__(self, rpm=700, kph=0, devctl_reply=("EE03",),
                 speed_seq=None):
        self.rpm, self.kph, self.devctl_reply = rpm, kph, devctl_reply
        self.speed_seq = list(speed_seq or [])
        self.calls, self.q = [], []

    def open(self): self.calls.append("open")
    def close(self): self.calls.append("close")
    def connect(self, proto, baud, flags=0): return 7
    def disconnect(self, ch): self.calls.append(("disconnect", ch))
    def pass_filter(self, ch, proto, patt, mask): pass

    def write(self, ch, tx_id, data, proto=None, txflags=None, timeout=200):
        p = data[1:1 + (data[0] & 0x0F)].hex().upper()
        self.calls.append(("tx", f"{tx_id:03X}", p))
        if tx_id == 0x7E0 and p == "010C":
            v = int(self.rpm * 4)
            self.q.append(fr(0x7E8, f"04410C{v:04X}000000"))
        elif tx_id == 0x7E0 and p == "010D":
            k = self.speed_seq.pop(0) if self.speed_seq else self.kph
            self.q.append(fr(0x7E8, f"03410D{k:02X}00000000"))
        elif tx_id == 0x7E4 and p.startswith("2C"):
            self.q.append(fr(0x7EC, f"026C{p[2:4]}0000000000"))
        elif tx_id == 0x7E4 and p.startswith("AA03"):
            self.q.append(fr(0x5EC, "FE01770176000000"))
        elif tx_id == 0x7E4 and p == "AE0302020000":
            for r in self.devctl_reply:
                n = len(bytes.fromhex(r))
                self.q.append(fr(0x7EC, (f"{n:02X}" + r).ljust(16, "0")))
            self.q.append(fr(0x5EC, "FE02420240000000"))

    def read(self, ch, timeout=30, max_msgs=64):
        out, self.q = self.q, []
        return out


def sent(fake):
    return [(c[1], c[2]) for c in fake.calls if isinstance(c, tuple) and c[0] == "tx"]


def clock_ticks(step=0.3):
    t = [0.0]

    def c():
        t[0] += step
        return t[0]
    return c


def yes(plan, phrase):
    return True


def no(plan, phrase):
    return False


# -- import ------------------------------------------------------------------ #
def test_import_extracts_the_autel_exchange_with_its_outcome():
    recs = [{"t": 13.521, "id": "7E4", "data": "06AE030202000000"},
            {"t": 13.522, "id": "7EC", "data": "02EE030000000000"},
            {"t": 14.924, "id": "7EC", "data": "057FAEE3000A0000"},
            {"t": 9.0, "id": "7E4", "data": "013E"}]
    ex = dc.import_capture(recs)
    assert ex == [{"module": "7E4", "request": "AE0302020000",
                   "response": "7FAEE3000A", "outcome": "negative:E3",
                   "t": 13.521}]


def test_import_reassembles_multiframe_requests_and_ignores_reads():
    recs = [{"t": 1.0, "id": "7E4", "data": "100AAE0501020304"},   # FF, len 10
            {"t": 1.01, "id": "7E4", "data": "2105060708000000"},  # CF
            {"t": 1.02, "id": "7EC", "data": "02EE050000000000"},
            {"t": 2.0, "id": "7E4", "data": "03223114"}]           # a read
    ex = dc.import_capture(recs)
    assert len(ex) == 1
    assert ex[0]["request"] == "AE050102030405060708"
    assert ex[0]["outcome"] == "positive"


def test_merge_is_idempotent():
    cat = {"controls": []}
    ex = [{"module": "7E4", "request": "AE0302020000", "response": None,
           "outcome": "no-answer", "t": 1}]
    assert dc.merge_into_catalog(cat, ex, "a.jsonl") == ["7E4-AE03-1"]
    assert dc.merge_into_catalog(cat, ex, "b.jsonl") == []
    assert cat["controls"][0]["status"] == "captured"


# -- refusals before anything actuates -------------------------------------- #
def test_engine_off_refuses_without_sending_the_control():
    fake = FakeTruck(rpm=0)
    res = dc.run_control(fake, ENTRY, yes, clock=clock_ticks())
    assert res["sent"] is False and "engine not running" in res["aborted"]
    assert ("7E4", "AE0302020000") not in sent(fake)


def test_moving_vehicle_refuses():
    res = dc.run_control(FakeTruck(kph=5), ENTRY, yes, clock=clock_ticks())
    assert res["sent"] is False and "moving" in res["aborted"]


def test_no_confirmation_refuses():
    fake = FakeTruck()
    res = dc.run_control(fake, ENTRY, no, clock=clock_ticks())
    assert res["sent"] is False and res["aborted"] == "not confirmed"
    assert ("7E4", "AE0302020000") not in sent(fake)


def test_non_devctl_entry_and_unknown_module_refused():
    with pytest.raises(dc.Refused):
        dc.run_control(FakeTruck(), {**ENTRY, "request": "0400"}, yes)
    with pytest.raises(dc.Refused):
        dc.run_control(FakeTruck(), {**ENTRY, "module": "7E7"}, yes)
    with pytest.raises(dc.Refused):
        dc.run_control(FakeTruck(), {**ENTRY, "request": "AE" + "00" * 8}, yes)


def test_tty_confirm_refuses_without_a_terminal(monkeypatch):
    class NotTty:
        def isatty(self): return False
    monkeypatch.setattr(dc.sys, "stdin", NotTty())
    assert dc.tty_confirm("plan", "ACTUATE X") is False


# -- the run ------------------------------------------------------------------ #
def test_run_sends_exact_bytes_streams_and_always_returns_control():
    fake = FakeTruck(devctl_reply=("EE03",))
    res = dc.run_control(fake, ENTRY, yes, seconds=1.0, clock=clock_ticks())
    assert res["sent"] is True and res["outcome"] == "positive"
    assert res["returned_to_normal"] is True
    s = sent(fake)
    assert ("7E4", "AE0302020000") in s
    assert s[-2:] == [("7E4", "20"), ("7E4", "AA00")]
    assert {"3114": 0x242, "3115": 0x240} == {k: v for k, v in res["samples"][-1].items()
                                              if k in ("3114", "3115")}


def test_module_refusal_stops_and_returns_control_with_detail():
    fake = FakeTruck(devctl_reply=("EE03", "7FAEE3000A"))
    res = dc.run_control(fake, ENTRY, yes, seconds=5.0, clock=clock_ticks())
    assert res["outcome"] == "negative:E3" and res["detail"] == "000A"
    assert sent(fake)[-2:] == [("7E4", "20"), ("7E4", "AA00")]


def test_motion_during_run_aborts_and_returns_control():
    fake = FakeTruck(speed_seq=[0, 0, 3, 3, 3])
    res = dc.run_control(fake, ENTRY, yes, seconds=10.0, clock=clock_ticks(0.5))
    assert res["sent"] is True and "speed" in res["aborted"]
    assert sent(fake)[-2:] == [("7E4", "20"), ("7E4", "AA00")]


def test_allowlist_blocks_everything_else_on_the_wire():
    fake = FakeTruck()
    seen = []
    orig = dc.Wire.send

    def spy(self, cid, payload):
        seen.append((cid, payload))
        return orig(self, cid, payload)
    dc.Wire.send = spy
    try:
        dc.run_control(fake, ENTRY, yes, seconds=0.5, clock=clock_ticks())
        w = dc.Wire(fake, lambda c, p: False, [0x7EC])
        w.jt = __import__("openobd.j2534", fromlist=["CAN"])
        w.ch = 1
        with pytest.raises(dc.Refused):
            orig(w, 0x7E4, bytes.fromhex("AE0500"))
    finally:
        dc.Wire.send = orig
    for cid, p in seen:
        assert (cid == 0x7E4 and (p in (bytes.fromhex("AE0302020000"), b"\x3e",
                                        b"\x20", b"\xaa\x00")
                                  or p[:1] in (b"\x2c", b"\xaa"))) or \
            (cid == 0x7E0 and p in (b"\x01\x0c", b"\x01\x0d"))


def test_list_is_numbered_and_named(capsys):
    assert dc.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "[1]" in out and "Autel TCCM learn" in out
    assert "TCCM (transfer case)" in out and "Enter the NUMBER" in out


def test_run_accepts_the_list_number(monkeypatch):
    seen = {}

    def fake_run(j, entry, confirm, seconds=None):
        seen["id"] = entry["id"]
        return {"sent": False, "outcome": None, "aborted": "not confirmed",
                "returned_to_normal": True, "samples": []}

    class J:
        pass
    monkeypatch.setattr(dc, "run_control", fake_run)
    import openobd.j2534 as jt
    monkeypatch.setattr(jt, "J2534", lambda *a, **k: J())
    monkeypatch.chdir(pathlib.Path(__file__).resolve().parent)
    dc.main(["--run", "1"])
    assert seen["id"] == "7E4-AE03-1"
    for f in pathlib.Path(__file__).resolve().parent.glob("devctl-7E4-AE03-1-*.json"):
        f.unlink()


def test_shipped_catalog_has_the_captured_tccm_control():
    cat = dc.load_catalog()
    ids = {c["id"]: c for c in cat["controls"]}
    assert ids["7E4-AE03-1"]["request"] == "AE0302020000"
    assert ids["7E4-AE03-1"]["status"] == "captured"
