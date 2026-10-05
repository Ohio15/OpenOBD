"""
tccmprobe.py -- precise, NON-ACTUATING interrogation of the transfer case
control module (TCCM, 7E4 -> 7EC, UUDT reports on 0x5EC) over the GT's
pass-thru driver.

Two questions, answered by the module itself:

1. WHY did it set C0398?  GMLAN $12 ReadFailureRecordData: $12 01 lists the
   failure records the module stored when it set a code; $12 02 reads one
   record back with the parameter bytes it froze at that moment. (Service and
   reply format verified on this truck's EBCM by truck-mcp, 2026-08-03.)

2. WHAT does it see during a shift, sampled together?  $2C
   DynamicallyDefineMessage packs identifiers into a DPID, $AA streams the
   DPIDs on UUDT 0x5EC. Both position readings ride in ONE DPID, so they are
   sampled at the same instant (didscan --watch reads them ~33 ms apart). This
   is exactly how the Autel streamed this module's data on 2026-10-04
   (2C FE..FA, AA 03 FE FD FC FB FA, captured by canrec).

Non-actuating by construction: every request passes ALLOWED_SIDS = {$12, $1A,
$22, $2C, $AA, $3E} before it reaches the wire, and $12 is further limited to
sub-functions 01/02. Device control ($AE), clears ($04/$14), security ($27),
sessions ($10/$28/$A5) and programming ($34/$36/$3B) are refused in code.
$2C/$AA only define and send a RAM-resident report; the stream is stopped
($AA 00) on every exit path.

Raw CAN (500 kbps) on ONE channel: the GT has refused a second simultaneous
channel before, and DPID packets are single raw UUDT frames, not ISO-TP. This
module does its own ISO-TP: single-frame requests, flow control for a
multi-frame reply. One device session per run.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Callable, Optional

REQ_ID, RESP_ID, UUDT_ID = 0x7E4, 0x7EC, 0x5EC
ALLOWED_SIDS = frozenset({0x12, 0x1A, 0x22, 0x2C, 0xAA, 0x3E})
ALLOWED_12_SUBS = frozenset({0x01, 0x02})

#: DPID layout. Each DPID is one 8-byte UUDT frame: the DPID byte + 7 data
#: bytes, so every identifier in a DPID is sampled together. FE carries BOTH
#: position readings (2 bytes each) -- the C0398 correlation pair.
#: Two identifiers per DPID so each $2C fits ONE single frame (2C + DPID + 2x2
#: id bytes = 6) and the $AA start for all five fits one frame too.
DPIDS = {
    0xFE: ["3114", "3115"],     # position A + position B, SAME instant
    0xFD: ["3142", "3140"],     # knob request + status
    0xFC: ["312A", "3176"],
    0xFB: ["3117", "3121"],
    0xFA: ["3107", "311A"],
}
DID_SIZE = {"3114": 2, "3115": 2}               # all others 1 byte (discovery)


class NotAllowed(ValueError):
    """A request outside the non-actuating allowlist was about to be sent."""


def check_allowed(payload: bytes) -> None:
    if not payload or payload[0] not in ALLOWED_SIDS:
        raise NotAllowed(f"refusing {payload.hex().upper()}: tccmprobe sends "
                         "$12/$1A/$22/$2C/$AA/$3E only")
    if payload[0] == 0x12 and (len(payload) < 2
                               or payload[1] not in ALLOWED_12_SUBS):
        raise NotAllowed(f"refusing {payload.hex().upper()}: $12 sub-functions "
                         "01/02 only")


def single_frame(payload: bytes) -> bytes:
    """ISO-TP single frame, padded to 8 bytes. Requests here never exceed 7."""
    if not 1 <= len(payload) <= 7:
        raise ValueError(f"single-frame payload must be 1..7 bytes, got {len(payload)}")
    return (bytes([len(payload)]) + payload).ljust(8, b"\x00")


def define_dpid_request(dpid: int, dids: list) -> bytes:
    """$2C <dpid> <did>... -- the same form the Autel sent this module."""
    if len(dids) > 2:
        raise ValueError("at most 2 identifiers per DPID (single frame)")
    return bytes([0x2C, dpid]) + b"".join(bytes.fromhex(d) for d in dids)


def decode_dpid(frame_data: bytes) -> Optional[dict]:
    """One UUDT frame (8 bytes) -> {did: int} for a known DPID, else None."""
    if not frame_data or frame_data[0] not in DPIDS:
        return None
    out, pos = {}, 1
    for did in DPIDS[frame_data[0]]:
        n = DID_SIZE.get(did, 1)
        if pos + n > len(frame_data):
            break
        out[did] = int.from_bytes(frame_data[pos:pos + n], "big")
        pos += n
    return out


def parse_failure_list(payload: bytes) -> dict:
    """'52 01' + count + [record, dtc-hi, dtc-lo, symptom]*."""
    if len(payload) >= 3 and payload[0] == 0x7F:
        return {"kind": "negative", "nrc": f"{payload[2]:02X}", "records": []}
    if len(payload) < 3 or payload[:2] != b"\x52\x01":
        return {"kind": "unexpected", "payload": payload.hex().upper(),
                "records": []}
    count, recs = payload[2], []
    for i in range(count):
        c = payload[3 + 4 * i: 7 + 4 * i]
        if len(c) < 4:
            break
        recs.append({"record": c[0], "dtc": _dtc(c[1], c[2]),
                     "dtc_hex": c[1:3].hex().upper(), "symptom": f"{c[3]:02X}"})
    return {"kind": "list", "count": count, "records": recs}


def parse_failure_record(payload: bytes) -> dict:
    """'52 02' + record + dtc-hi + dtc-lo + symptom + frozen parameter bytes."""
    if len(payload) >= 3 and payload[0] == 0x7F:
        return {"kind": "negative", "nrc": f"{payload[2]:02X}"}
    if len(payload) < 6 or payload[:2] != b"\x52\x02":
        return {"kind": "unexpected", "payload": payload.hex().upper()}
    return {"kind": "record", "record": payload[2],
            "dtc": _dtc(payload[3], payload[4]), "symptom": f"{payload[5]:02X}",
            "parameters_hex": payload[6:].hex().upper()}


def _dtc(hi: int, lo: int) -> str:
    v = (hi << 8) | lo
    return f"{'PCBU'[v >> 14]}{(v >> 12) & 3}{v & 0xFFF:03X}"


class RawSession:
    """One raw-CAN channel to the TCCM: allowlisted requests, own ISO-TP RX."""

    def __init__(self, j, timeout_s: float = 1.0):
        self.j, self.timeout_s, self.ch = j, timeout_s, None
        self.stream_frames: list = []        # UUDT frames seen while waiting

    def __enter__(self):
        from . import j2534 as jt
        self.jt = jt
        self.j.open()
        try:
            self.ch = self.j.connect(jt.CAN, 500000)
            self.j.pass_filter(self.ch, jt.CAN, RESP_ID, 0x7FF)
            self.j.pass_filter(self.ch, jt.CAN, UUDT_ID, 0x7FF)
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        if self.ch is not None:
            try:
                self.j.disconnect(self.ch)
            except Exception:                                 # noqa: BLE001
                pass
            self.ch = None
        try:
            self.j.close()
        except Exception:                                     # noqa: BLE001
            pass

    def __exit__(self, *exc):
        self.close()
        return False

    def send(self, payload: bytes) -> None:
        check_allowed(payload)
        self.j.write(self.ch, REQ_ID, single_frame(payload),
                     proto=self.jt.CAN, txflags=0)

    def _frames(self):
        for data in self.j.read(self.ch, timeout=50, max_msgs=64):
            if len(data) < 5:
                continue
            yield int.from_bytes(data[:4], "big") & 0x7FF, data[4:]

    def wait_negative(self, sid: int, seconds: float) -> Optional[int]:
        """Watch the response id for `seconds`; return an NRC for `sid` if one
        arrives (stream frames seen meanwhile are kept), else None."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for cid, d in self._frames():
                if cid == UUDT_ID:
                    self.stream_frames.append(d)
                elif cid == RESP_ID and len(d) >= 4 and d[1] == 0x7F \
                        and d[2] == sid:
                    return d[3]
        return None

    def request(self, payload: bytes) -> Optional[bytes]:
        """Send an allowlisted request, return the reassembled reply payload
        (no PCI) for THIS service, or None on silence. Handles $78 pending and
        a multi-frame reply (sends the flow control itself)."""
        self.send(payload)
        sid = payload[0]
        end = time.monotonic() + self.timeout_s
        buf, need = b"", None
        while time.monotonic() < end:
            for cid, d in self._frames():
                if cid == UUDT_ID:
                    self.stream_frames.append(d)
                    continue
                if cid != RESP_ID or not d:
                    continue
                pci = d[0] >> 4
                if pci == 0:                                  # single frame
                    msg = d[1:1 + (d[0] & 0x0F)]
                    if msg[:1] == b"\x7f" and len(msg) >= 3 and msg[1] == sid:
                        if msg[2] == 0x78:
                            end = time.monotonic() + 5.0
                            continue
                        return msg
                    if msg[:1] == bytes([(sid + 0x40) & 0xFF]):
                        return msg
                elif pci == 1:                                # first frame
                    need = ((d[0] & 0x0F) << 8) | d[1]
                    buf = d[2:]
                    self.j.write(self.ch, REQ_ID,
                                 b"\x30\x00\x00".ljust(8, b"\x00"),
                                 proto=self.jt.CAN, txflags=0)
                    end = time.monotonic() + self.timeout_s
                elif pci == 2 and need is not None:           # consecutive
                    buf += d[1:]
                    if len(buf) >= need:
                        return buf[:need]
        return None


def read_failure_records(sess: RawSession) -> dict:
    lst = sess.request(b"\x12\x01")
    if lst is None:
        return {"error": "no answer to $12 01", "list": None, "records": []}
    parsed = parse_failure_list(lst)
    out = {"error": None, "list": parsed, "records": []}
    for r in parsed.get("records", []):
        req = bytes([0x12, 0x02, r["record"]]) + bytes.fromhex(r["dtc_hex"]) \
            + bytes.fromhex(r["symptom"])
        rep = sess.request(req)
        out["records"].append(parse_failure_record(rep) if rep is not None
                              else {"kind": "silent", "dtc": r["dtc"]})
    return out


def stream(sess: RawSession, seconds: float,
           on_sample: Callable[[float, dict], None],
           clock: Callable[[], float] = time.monotonic) -> dict:
    """Define the DPIDs, stream them, hand each decoded sample to on_sample,
    and ALWAYS stop the stream. Returns {"samples", "defined", "error"}."""
    out = {"samples": 0, "defined": [], "error": None, "rate": None}
    try:
        for dpid, dids in DPIDS.items():
            rep = sess.request(define_dpid_request(dpid, dids))
            if rep is None or rep[:1] != b"\x6c":
                out["error"] = (f"module refused DPID {dpid:02X}: "
                                f"{rep.hex().upper() if rep else 'silence'}")
                return out
            out["defined"].append(f"{dpid:02X}")
        # $AA 04 = fast rate; the Autel used 03 (medium, ~300 ms). If the
        # module NAKs fast, fall back to medium.
        for rate in (0x04, 0x03):
            sess.send(bytes([0xAA, rate]) + bytes(DPIDS))
            nak = sess.wait_negative(0xAA, 0.5)
            if nak is None:
                out["rate"] = f"{rate:02X}"
                break
        else:
            out["error"] = "module refused $AA at fast and medium rate"
            return out
        t0 = clock()
        last_tp = t0
        for d in sess.stream_frames:                  # arrived during the NAK wait
            v = decode_dpid(d)
            if v:
                out["samples"] += 1
                on_sample(0.0, v)
        sess.stream_frames.clear()
        while clock() - t0 < seconds:
            for cid, d in sess._frames():
                if cid == UUDT_ID:
                    v = decode_dpid(d)
                    if v:
                        out["samples"] += 1
                        on_sample(clock() - t0, v)
            if clock() - last_tp >= 1.0:              # keep the session alive
                sess.send(b"\x3e")
                last_tp = clock()
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"stream failed: {e}"
    finally:
        try:
            sess.send(b"\xaa\x00")                    # stop sending
        except Exception:                                     # noqa: BLE001
            pass
    return out


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="python -m openobd.tccmprobe")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--failure-records", action="store_true")
    mode.add_argument("--stream", metavar="LABEL")
    ap.add_argument("--seconds", type=float, default=30.0)
    a = ap.parse_args(argv)
    from . import j2534 as jt
    try:
        j = jt.J2534()
    except Exception as e:                                    # noqa: BLE001
        print(f"pass-thru driver not available: {e}")
        return 3
    try:
        with RawSession(j) as sess:
            if a.failure_records:
                res = read_failure_records(sess)
                with open("tccm-failure-records.json", "w", encoding="utf-8") as fh:
                    json.dump(res, fh, indent=1)
                if res["error"]:
                    print(f"NOT EXAMINED -- {res['error']}")
                    return 3
                lst = res["list"]
                if lst["kind"] != "list":
                    print(f"$12 01 answer: {lst}")
                    return 3
                print(f"{lst['count']} failure record(s) stored in the TCCM:")
                for r in res["records"]:
                    print(f"  {r}")
                print("-> tccm-failure-records.json")
            else:
                outp = f"tccm-stream-{a.stream}.csv"
                cols = ["t_s"] + [d for ds in DPIDS.values() for d in ds]
                latest: dict = {}
                print(f"streaming {len(cols) - 1} values for {a.seconds:.0f} s "
                      f"-> {outp}. TURN THE KNOB NOW.")
                with open(outp, "w", encoding="utf-8") as fh:
                    fh.write(",".join(cols) + "\n")

                    def on_sample(t, v):
                        latest.update(v)
                        fh.write(f"{t:.3f}," + ",".join(
                            str(latest.get(c, "")) for c in cols[1:]) + "\n")
                        fh.flush()
                    res = stream(sess, a.seconds, on_sample)
                print(f"done: {res['samples']} samples, DPIDs {res['defined']}"
                      + (f"  [{res['error']}]" if res["error"] else ""))
    except Exception as e:                                    # noqa: BLE001
        print(f"probe failed: {e}")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
