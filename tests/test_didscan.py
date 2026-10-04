"""Read-only DID discovery (openobd.didscan) against a fake pass-thru client."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from openobd import didscan  # noqa: E402


def fr(cid, payload_hex):
    return cid.to_bytes(4, "big") + bytes.fromhex(payload_hex)


class FakeModule:
    """Pass-thru stand-in for one module: answers $22/$1A from a table, NRC 31
    otherwise; optional pending-then-answer and silence."""
    def __init__(self, dids=None, idents=None, silent=(), pending=(),
                 resp=0x7EC):
        self.dids, self.idents = dids or {}, idents or {}
        self.silent, self.pending = set(silent), set(pending)
        self.resp, self.calls, self.q = resp, [], []

    def open(self): self.calls.append("open")
    def close(self): self.calls.append("close")
    def connect(self, proto, baud, flags=0):
        self.calls.append(("connect", proto, baud)); return 3
    def disconnect(self, ch): self.calls.append(("disconnect", ch))
    def flow_control_filter(self, ch, tx, rx, proto=None):
        self.calls.append(("fc", tx, rx))

    def write(self, ch, tx_id, payload, proto=None, txflags=None, timeout=200):
        self.calls.append(("write", tx_id, payload.hex().upper()))
        sid = payload[0]
        if sid == 0x22:
            did = int.from_bytes(payload[1:3], "big")
            if did in self.silent:
                return
            if did in self.pending:
                self.q.append(fr(self.resp, "7F2278"))
            if did in self.dids:
                self.q.append(fr(self.resp, f"62{did:04X}{self.dids[did]}"))
            else:
                self.q.append(fr(self.resp, "7F2231"))
        elif sid == 0x1A:
            i = payload[1]
            if i in self.idents:
                self.q.append(fr(self.resp, f"5A{i:02X}{self.idents[i]}"))
            else:
                self.q.append(fr(self.resp, "7F1A31"))

    def read(self, ch, timeout=200, max_msgs=8):
        out, self.q = self.q, []
        return out


def sess(fake, timeout_ms=20):
    return didscan.Session(fake, 0x7E4, 0x7EC, timeout_ms=timeout_ms)


def test_classify():
    req = didscan.did_request(0x1234)
    assert didscan.classify(req, bytes.fromhex("621234AABB")) == ("ok", b"\xaa\xbb")
    assert didscan.classify(req, bytes.fromhex("621235AABB")) is None   # other DID
    assert didscan.classify(req, bytes.fromhex("7F2231")) == ("nrc", 0x31)
    assert didscan.classify(req, bytes.fromhex("7F2278")) == ("pending", None)
    assert didscan.classify(req, bytes.fromhex("7F1A31")) is None       # other svc


def test_sender_refuses_anything_but_reads():
    with sess(FakeModule()) as s:
        for bad in ("04", "14FFFFFF", "AE01", "3101FF00", "2E123400", "10 02"):
            with pytest.raises(didscan.NotReadOnly):
                s.query(bytes.fromhex(bad.replace(" ", "")))


def test_discover_finds_supported_and_counts_nrc_and_silence():
    fake = FakeModule(dids={0x0010: "01", 0x00A3: "7F40"},
                      idents={0x90: "414243"}, silent={0x0005})
    hits = []
    with sess(fake) as s:
        res = didscan.discover(s, 0x0000, 0x00FF,
                               on_hit=lambda k, v: hits.append(k))
    assert res["hits"] == {"1A:90": "414243", "22:0010": "01", "22:00A3": "7F40"}
    assert hits == ["1A:90", "22:0010", "22:00A3"]
    assert res["silent"] == 1 and res["nrc"]["31"] == 255 + 253
    assert res["last"] == 0x00FF


def test_pending_then_answer_is_a_hit():
    fake = FakeModule(dids={0x0020: "AA"}, pending={0x0020})
    with sess(fake) as s:
        res = didscan.discover(s, 0x0020, 0x0020, idents=False)
    assert res["hits"] == {"22:0020": "AA"}


def test_only_read_services_reach_the_wire():
    fake = FakeModule(dids={0x0001: "00"})
    with sess(fake) as s:
        didscan.discover(s, 0x0000, 0x0003)
    sent = [c[2][:2] for c in fake.calls if isinstance(c, tuple) and c[0] == "write"]
    assert sent and set(sent) <= {"22", "1A"}


def test_session_opens_once_and_closes_last():
    fake = FakeModule()
    with sess(fake) as s:
        didscan.read_keys(s, ["22:0001", "1A:90"])
    assert fake.calls[0] == "open" and fake.calls[-1] == "close"
    assert fake.calls.count("open") == 1 and fake.calls.count("close") == 1
    assert ("fc", 0x7E4, 0x7EC) in fake.calls


def test_read_keys_and_diff():
    a = FakeModule(dids={0x0010: "01", 0x0011: "55"})
    b = FakeModule(dids={0x0010: "02", 0x0011: "55"})
    with sess(a) as s:
        va = didscan.read_keys(s, ["22:0010", "22:0011"])
    with sess(b) as s:
        vb = didscan.read_keys(s, ["22:0010", "22:0011"])
    rows = didscan.diff({"2HI": va, "4HI": vb})
    assert rows == [("22:0010", {"2HI": "01", "4HI": "02"})]


def test_reply_from_another_module_is_ignored():
    fake = FakeModule(dids={0x0010: "01"}, resp=0x7E8)   # wrong responder
    with sess(fake) as s:
        res = didscan.discover(s, 0x0010, 0x0010, idents=False)
    assert res["hits"] == {} and res["silent"] == 1


def test_watch_records_passes_until_time_is_up():
    fake = FakeModule(dids={0x3114: "0271", 0x3115: "026F"})
    ticks = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    rows = []
    with sess(fake) as s:
        n = didscan.watch(s, ["22:3114", "22:3115"], 0.3,
                          lambda t, v: rows.append((t, v)),
                          clock=lambda: next(ticks))
    assert n == 3 and len(rows) == 3
    assert rows[0][1] == {"22:3114": "0271", "22:3115": "026F"}
    sent = [c[2][:2] for c in fake.calls if isinstance(c, tuple) and c[0] == "write"]
    assert set(sent) == {"22"}                          # read-only


def test_tccm_watch_set_is_small_and_reads_only():
    assert len(didscan.TCCM_WATCH) <= 6
    assert all(k.startswith("22:") for k in didscan.TCCM_WATCH)
    assert {"22:3114", "22:3115", "22:3142"} <= set(didscan.TCCM_WATCH)


def test_cli_diff(tmp_path, capsys):
    for lbl, v in (("2HI", "01"), ("4HI", "02")):
        (tmp_path / f"{lbl}.json").write_text(json.dumps(
            {"label": lbl, "values": {"22:0010": v, "22:0011": "55"}}))
    rc = didscan.main(["--diff", str(tmp_path / "2HI.json"),
                       str(tmp_path / "4HI.json")])
    out = capsys.readouterr().out
    assert rc == 0 and "1 identifier(s) differ" in out and "22:0010" in out
