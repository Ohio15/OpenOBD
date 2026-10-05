"""
devctl.py -- GATED device control (GM $AE) from a catalog of controls OBSERVED
on this truck. Ron-run only; it moves hardware.

WHERE CONTROLS COME FROM. GM modules have no "list your device controls"
service, and the only way to probe for one is to send it -- a supported control
actuates the moment it arrives. So nothing here invents or guesses a control.
`--import` reads a canrec capture of another tool (the Autel on the Y-splitter)
and extracts every $AE exchange it saw; those land in openobd/data/
controls.json as status "captured" until they have run here.

WHAT RUNS. `--run ID` replays the catalog entry's request BYTE FOR BYTE. Before
sending it:
  * interlocks are READ from the vehicle: engine running (ECM PID 0C) and
    vehicle stopped (ECM PID 0D == 0); the transmission in Neutral cannot be
    measured on this path and is ATTESTED by the operator in the confirmation;
  * the operator types an exact confirmation phrase at an interactive terminal
    (refused when stdin is not a TTY -- no script can approve an actuation).
While it runs: TesterPresent every 1 s, vehicle speed re-read every 1 s (any
motion aborts), and for the TCCM the position pair is streamed ($2C/$AA, both
readings in one DPID). On EVERY exit -- done, timeout, module refusal, motion,
Ctrl+C, error -- it sends GMLAN $20 ReturnToNormalMode to the module, which
cancels device control and hands the actuator back, then stops the stream.
Every frame sent and received is written to devctl-<id>-<time>.jsonl.

The wire allowlist for a run is: the exact catalog request, $3E, $20, the
$2C/$AA stream setup for that module, and OBD mode 01 PIDs 0C/0D to the ECM.
Anything else is refused before it reaches the bus.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Callable, Optional

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "data", "controls.json")
ECM_REQ, ECM_RESP = 0x7E0, 0x7E8
SID_DEVCTL = 0xAE

#: HS request id -> (response id, UUDT id, name) for modules known on this truck
MODULES = {0x7E4: (0x7EC, 0x5EC, "TCCM (transfer case)"),
           0x7E2: (0x7EA, 0x5EA, "TCM (transmission)"),
           0x7E0: (0x7E8, 0x5E8, "ECM (engine)"),
           0x243: (0x643, 0x543, "EBCM (ABS)"),
           0x241: (0x641, 0x541, "BCM (body)")}


def module_info(bus: str, req: int) -> Optional[tuple]:
    """(response id, UUDT id, name) for a module on a bus, or None. The SW
    body modules are not identified yet, so they are named by address only --
    and 0x243 on SW is NOT the EBCM (that is 0x243 on HS)."""
    if bus == "hs":
        return MODULES.get(req)
    if bus == "sw" and 0x240 <= req <= 0x25F:
        return (req + 0x400, req + 0x300, f"body-bus module 0x{req:03X}")
    return None


class Refused(Exception):
    """The run was refused before anything actuating was sent."""


# --------------------------------------------------------------------------- #
# Capture import: canrec JSONL -> device-control exchanges
# --------------------------------------------------------------------------- #
def _reassemble(frames: list) -> list:
    """[(t, id_int, data_bytes)] for ONE CAN id -> [(t, payload)] messages
    (ISO-TP: single frames and first+consecutive frames)."""
    out, buf, need, t0 = [], b"", None, None
    for t, _cid, d in frames:
        if not d:
            continue
        pci = d[0] >> 4
        if pci == 0:
            n = d[0] & 0x0F
            if 0 < n <= len(d) - 1:
                out.append((t, d[1:1 + n]))
        elif pci == 1:
            need = ((d[0] & 0x0F) << 8) | d[1]
            buf, t0 = d[2:], t
        elif pci == 2 and need is not None:
            buf += d[1:]
            if len(buf) >= need:
                out.append((t0, buf[:need]))
                need = None
    return out


def _resp_id(req: int) -> Optional[int]:
    if 0x7E0 <= req <= 0x7E7:
        return req + 8
    if 0x240 <= req <= 0x25F:
        return req + 0x400
    return None


def import_capture(records: list, window_s: float = 3.0) -> list:
    """canrec records [{t, id, data}] -> every $AE exchange:
    [{module, request, response, outcome, t}] where outcome is 'positive',
    'negative:<NRC>' or 'no-answer'."""
    by_id: dict = {}
    for r in records:
        bus = r.get("bus", "hs")             # pre-0.30 recordings were HS only
        cid = int(r["id"], 16)
        by_id.setdefault((bus, cid), []).append(
            (r["t"], cid, bytes.fromhex(r["data"])))
    msgs = {k: _reassemble(f) for k, f in by_id.items()}
    found = []
    for (bus, req), reqs in msgs.items():
        rid = _resp_id(req)
        if rid is None:
            continue
        for t, p in reqs:
            if not p or p[0] != SID_DEVCTL:
                continue
            outcome, resp = "no-answer", None
            for rt, rp in msgs.get((bus, rid), []):
                if rt < t or rt > t + window_s or not rp:
                    continue
                if rp[0] == 0xEE and rp[1:2] == p[1:2]:
                    outcome, resp = "positive", rp
                    # keep looking: a later negative (abort) overrides
                elif rp[0] == 0x7F and len(rp) >= 3 and rp[1] == SID_DEVCTL:
                    if rp[2] == 0x78:
                        # responsePending: the module is still working on it.
                        # Not an answer -- keep looking for the final one.
                        if outcome == "no-answer":
                            outcome, resp = "pending-only", rp
                        continue
                    outcome, resp = f"negative:{rp[2]:02X}", rp
                    break
            found.append({"bus": bus, "module": f"{req:03X}",
                          "request": p.hex().upper(),
                          "response": resp.hex().upper() if resp else None,
                          "outcome": outcome, "t": t})
    return sorted(found, key=lambda x: x["t"])


#: negative response codes a device control can return (ISO 14229 / GMW3110)
NRC_TEXT = {"11": "service not supported", "12": "sub-function not supported",
            "22": "conditions not correct", "31": "request out of range",
            "33": "security access denied", "78": "busy (response pending)",
            "E3": "device control limits exceeded"}


def describe_outcome(outcome: Optional[str]) -> str:
    if outcome == "positive":
        return "module accepted it"
    if outcome == "no-answer" or not outcome:
        return "module did not answer"
    if outcome == "pending-only":
        return "module said 'busy, wait' and gave no final answer in the capture"
    if outcome.startswith("negative:"):
        code = outcome.split(":", 1)[1]
        return f"module refused it ({NRC_TEXT.get(code, 'code ' + code)})"
    return outcome


def load_catalog(path: str = CATALOG_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"controls": []}


def merge_into_catalog(catalog: dict, exchanges: list, source: str) -> list:
    """Add each new (module, request) as status 'captured'. Returns new ids."""
    have = {(c.get("bus", "hs"), c["module"], c["request"])
            for c in catalog["controls"]}
    new = []
    for ex in exchanges:
        bus = ex.get("bus", "hs")
        key = (bus, ex["module"], ex["request"])
        if key in have:
            for c in catalog["controls"]:
                if (c.get("bus", "hs"), c["module"], c["request"]) == key:
                    tally = c.setdefault("observed_counts", {})
                    tally[ex["outcome"]] = tally.get(ex["outcome"], 0) + 1
            continue
        cpid = ex["request"][2:4]
        prefix = "" if bus == "hs" else "SW-"
        cid = f"{prefix}{ex['module']}-AE{cpid}-{len(catalog['controls']) + 1}"
        catalog["controls"].append({
            "id": cid, "bus": bus, "module": ex["module"],
            "request": ex["request"],
            "cpid": cpid, "name": "(unnamed -- name it from the tool's menu)",
            "status": "captured", "source": source,
            "observed_outcome": ex["outcome"], "observed_response": ex["response"],
            "observed_counts": {ex["outcome"]: 1},
            "max_seconds": 20})
        have.add(key)
        new.append(cid)
    return new


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def single_frame(payload: bytes) -> bytes:
    if not 1 <= len(payload) <= 7:
        raise Refused(f"request is {len(payload)} bytes; only single-frame "
                      "(1..7) requests are supported")
    return (bytes([len(payload)]) + payload).ljust(8, b"\x00")


class Wire:
    """Raw CAN channel with a per-run allowlist of exact (id, payload)
    prefixes. Every frame in and out is appended to `log`."""

    def __init__(self, j, allow: Callable[[int, bytes], bool], ids: list,
                 clock: Callable[[], float] = time.monotonic, bus: str = "hs"):
        self.j, self.allow, self.ids, self.clock = j, allow, ids, clock
        self.bus = bus
        self.ch, self.log, self.t0 = None, [], clock()

    def _connect(self, bus: str, ids: list) -> None:
        jt = self.jt
        if bus == "sw":
            self.ch = self.j.connect(jt.SW_CAN_PS, jt.SW_CAN_BAUD)
            self.j.set_config(self.ch, [(jt.J1962_PINS, jt.SW_CAN_PINS)])
            self.proto = jt.SW_CAN_PS
        else:
            self.ch = self.j.connect(jt.CAN, 500000)
            self.proto = jt.CAN
        for cid in ids:
            self.j.pass_filter(self.ch, self.proto, cid, 0x7FF)
        self.bus, self.ids = bus, ids

    def use_bus(self, bus: str, ids: list) -> None:
        """Move to another bus inside the SAME device session (one open per
        process -- the OBDX driver can kill the process at close)."""
        if self.ch is not None:
            try:
                self.j.disconnect(self.ch)
            finally:
                self.ch = None
        self._connect(bus, ids)

    def __enter__(self):
        from . import j2534 as jt
        self.jt = jt
        self.j.open()
        try:
            self._connect(self.bus, self.ids)
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

    def send(self, cid: int, payload: bytes) -> None:
        if not self.allow(cid, payload):
            raise Refused(f"refusing {cid:03X} {payload.hex().upper()}: not in "
                          "this run's allowlist")
        self.j.write(self.ch, cid, single_frame(payload),
                     proto=self.proto, txflags=0)
        self.log.append({"t": round(self.clock() - self.t0, 4), "dir": "tx",
                         "bus": self.bus, "id": f"{cid:03X}",
                         "data": payload.hex().upper()})

    def frames(self):
        for d in self.j.read(self.ch, timeout=30, max_msgs=64):
            if len(d) < 5:
                continue
            cid = int.from_bytes(d[:4], "big") & 0x7FF
            self.log.append({"t": round(self.clock() - self.t0, 4), "dir": "rx",
                             "bus": self.bus, "id": f"{cid:03X}",
                             "data": d[4:].hex().upper()})
            yield cid, d[4:]

    def ask(self, cid: int, payload: bytes, resp: int,
            timeout_s: float = 0.6) -> Optional[bytes]:
        """Single-frame request -> single-frame reply payload (or None)."""
        self.send(cid, payload)
        end = self.clock() + timeout_s
        while self.clock() < end:
            for rid, d in self.frames():
                if rid == resp and d and d[0] >> 4 == 0:
                    msg = d[1:1 + (d[0] & 0x0F)]
                    if msg[:1] == bytes([(payload[0] + 0x40) & 0xFF]) or \
                            (msg[:1] == b"\x7f" and msg[1:2] == payload[:1]):
                        return msg
        return None


def read_interlocks(w: Wire) -> dict:
    """Engine speed and vehicle speed from the ECM (OBD mode 01)."""
    out = {"rpm": None, "kph": None}
    r = w.ask(ECM_REQ, b"\x01\x0c", ECM_RESP)
    if r and r[:2] == b"\x41\x0c" and len(r) >= 4:
        out["rpm"] = ((r[2] << 8) | r[3]) / 4
    r = w.ask(ECM_REQ, b"\x01\x0d", ECM_RESP)
    if r and r[:2] == b"\x41\x0d" and len(r) >= 3:
        out["kph"] = r[2]
    return out


def interlock_problems(il: dict) -> list:
    p = []
    if il["rpm"] is None:
        p.append("engine speed unreadable (ECM did not answer PID 0C)")
    elif il["rpm"] < 400:
        p.append(f"engine not running (rpm {il['rpm']:.0f})")
    if il["kph"] is None:
        p.append("vehicle speed unreadable (ECM did not answer PID 0D)")
    elif il["kph"] != 0:
        p.append(f"vehicle moving ({il['kph']} km/h)")
    return p


def confirm_phrase(entry: dict) -> str:
    return f"ACTUATE {entry['id']} IN NEUTRAL"


def tty_confirm(prompt: str, phrase: str) -> bool:
    """Exact phrase at an interactive terminal; anything else is a refusal."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    print(prompt)
    try:
        typed = input(f'Type exactly "{phrase}" to proceed: ')
    except EOFError:
        return False
    return typed.strip() == phrase


def run_control(j, entry: dict, confirm: Callable[[str, str], bool],
                seconds: Optional[float] = None,
                clock: Callable[[], float] = time.monotonic) -> dict:
    """Execute one catalog entry under interlocks + confirmation, ALWAYS
    returning control ($20) on exit. Returns a result record."""
    bus = entry.get("bus", "hs")
    req = int(entry["module"], 16)
    info = module_info(bus, req)
    if info is None:
        raise Refused(f"module {entry['module']} on bus {bus} is not a known "
                      "module here")
    resp, uudt, name = info
    request = bytes.fromhex(entry["request"])
    if not request or request[0] != SID_DEVCTL:
        raise Refused("catalog entry is not a device-control ($AE) request")
    single_frame(request)                       # refuses multi-frame up front
    seconds = min(float(seconds or entry.get("max_seconds", 20)), 60.0)
    stream = None
    if bus == "hs" and req == 0x7E4:
        from . import tccmprobe
        stream = tccmprobe

    def allow(cid: int, p: bytes) -> bool:
        if cid == req and p == request:
            return True
        if cid == req and p in (b"\x3e", b"\x20", b"\xaa\x00"):
            return True
        if cid == ECM_REQ and p in (b"\x01\x0c", b"\x01\x0d"):
            return True
        if stream and cid == req and p[:1] == b"\x2c" and \
                p[1] in stream.DPIDS and \
                p == stream.define_dpid_request(p[1], stream.DPIDS[p[1]]):
            return True
        if stream and cid == req and p[:1] == b"\xaa" and \
                p[2:] == bytes(stream.DPIDS) and p[1] in (0x03, 0x04):
            return True
        return False

    result = {"id": entry["id"], "bus": bus, "module": name,
              "request": entry["request"], "sent": False, "outcome": None,
              "aborted": None, "samples": [], "interlocks": None,
              "returned_to_normal": False, "frames": [],
              "motion_monitoring": bus == "hs"}
    # Interlocks come from the ECM, which is on HS. A body-bus control reads
    # them on HS first, then moves the SAME device session to the body bus;
    # on SW the vehicle speed cannot be re-read during the run.
    first_ids = [resp, uudt, ECM_RESP] if bus == "hs" else [ECM_RESP]
    w = Wire(j, allow, first_ids, clock=clock, bus="hs")
    with w:
        try:
            il = read_interlocks(w)
            result["interlocks"] = il
            probs = interlock_problems(il)
            if probs:
                result["aborted"] = "interlock: " + "; ".join(probs)
                return result
            plan = (f"\nDEVICE CONTROL -- this MOVES hardware.\n"
                    f"  module   {name} ({entry['module']})\n"
                    f"  request  {entry['request']}  ({entry.get('name')})\n"
                    f"  source   {entry.get('source')}  status {entry.get('status')}\n"
                    f"  engine   {il['rpm']:.0f} rpm, vehicle stopped\n"
                    f"  limit    {seconds:.0f} s, then control is returned ($20)\n"
                    f"  YOU confirm the transmission is in NEUTRAL, foot on the "
                    f"brake, nobody near the driveline.")
            if bus == "sw":
                plan += ("\n  NOTE: body-bus control -- vehicle speed is checked "
                         "before the run but CANNOT be re-read during it.")
            if not confirm(plan, confirm_phrase(entry)):
                result["aborted"] = "not confirmed"
                return result
            if bus == "sw":
                w.use_bus("sw", [resp, uudt])
            w.send(req, b"\x3e")
            if stream:
                for dpid, dids in stream.DPIDS.items():
                    w.ask(req, stream.define_dpid_request(dpid, dids), resp)
                w.send(req, bytes([0xAA, 0x03]) + bytes(stream.DPIDS))
            w.send(req, request)
            result["sent"] = True
            t0 = clock()
            last_tp = last_il = t0
            while clock() - t0 < seconds:
                for cid, d in w.frames():
                    if cid == uudt and stream:
                        v = stream.decode_dpid(d)
                        if v:
                            result["samples"].append(
                                {"t": round(clock() - t0, 3), **v})
                    elif cid == resp and d and d[0] >> 4 == 0:
                        msg = d[1:1 + (d[0] & 0x0F)]
                        if msg[:1] == b"\xee" and result["outcome"] is None:
                            result["outcome"] = "positive"
                        elif msg[:2] == b"\x7f\xae" and len(msg) >= 3:
                            result["outcome"] = f"negative:{msg[2]:02X}"
                            result["detail"] = msg[3:].hex().upper()
                            result["aborted"] = "module refused/aborted"
                            return result
                now = clock()
                if now - last_tp >= 1.0:
                    w.send(req, b"\x3e")
                    last_tp = now
                if bus == "hs" and now - last_il >= 1.0:
                    il = read_interlocks(w)
                    if il["kph"] is None or il["kph"] != 0:
                        result["aborted"] = f"interlock during run: speed {il['kph']}"
                        return result
                    last_il = now
            result["aborted"] = result["aborted"] or "time limit reached"
            return result
        except KeyboardInterrupt:
            result["aborted"] = "operator Ctrl+C"
            return result
        finally:
            for p in (b"\x20", b"\xaa\x00"):
                try:
                    w.send(req, p)
                    if p == b"\x20":
                        result["returned_to_normal"] = True
                except Exception:                             # noqa: BLE001
                    pass
            result["frames"] = w.log


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="python -m openobd.devctl")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--import", dest="imp", metavar="CANREC_JSONL")
    mode.add_argument("--list", action="store_true")
    mode.add_argument("--run", metavar="ID")
    ap.add_argument("--seconds", type=float)
    a = ap.parse_args(argv)
    cat = load_catalog()
    if a.list:
        if not cat["controls"]:
            print("No device controls captured yet -- record a tool's session "
                  "with _run_canrec.bat, then import it.")
            return 0
        print("DEVICE CONTROLS captured on this truck (each one MOVES hardware):")
        print()
        for n, c in enumerate(cat["controls"], 1):
            mi = module_info(c.get("bus", "hs"), int(c["module"], 16))
            mod = mi[2] if mi else c["module"]
            print(f"  [{n}]  {c['name']}")
            print(f"       module: {mod} ({'body bus' if c.get('bus') == 'sw' else 'main bus'})   ID: {c['id']}")
            counts = c.get("observed_counts") or {c["observed_outcome"]: 1}
            seen = "; ".join(f"{describe_outcome(k)} x{v}"
                             for k, v in counts.items())
            print(f"       seen: {seen}")
            print()
        print("Enter the NUMBER in brackets (e.g. 1) to run that control, "
              "or leave blank to cancel.")
        return 0
    if a.imp:
        with open(a.imp, encoding="utf-8") as fh:
            recs = [json.loads(l) for l in fh if l.strip()]
        ex = import_capture(recs)
        print(f"{len(ex)} device-control exchange(s) in {a.imp}:")
        for e in ex:
            print(f"  {e['t']:8.3f}s {e['module']} {e['request']} -> "
                  f"{e['response']} ({e['outcome']})")
        new = merge_into_catalog(cat, ex, os.path.basename(a.imp))
        with open(CATALOG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cat, fh, indent=1)
        print(f"added {len(new)} new control(s): {new}")
        return 0
    sel = a.run.strip()
    if sel.isdigit() and 1 <= int(sel) <= len(cat["controls"]):
        entry = cat["controls"][int(sel) - 1]
    else:
        entry = next((c for c in cat["controls"] if c["id"] == sel), None)
    if entry is None:
        print(f"no control {a.run!r} in the catalog (--list)")
        return 2
    from . import j2534 as jt
    try:
        j = jt.J2534()
    except Exception as e:                                    # noqa: BLE001
        print(f"pass-thru driver not available: {e}")
        return 3
    try:
        res = run_control(j, entry, tty_confirm, a.seconds)
    except Refused as e:
        print(f"REFUSED: {e}")
        return 2
    outp = f"devctl-{entry['id']}-{time.strftime('%Y%m%dT%H%M%S')}.json"
    with open(outp, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
    print(f"sent={res['sent']} outcome={res['outcome']} aborted={res['aborted']} "
          f"returned_to_normal={res['returned_to_normal']} "
          f"samples={len(res['samples'])} -> {outp}")
    return 0 if res["sent"] and not str(res["outcome"]).startswith("negative") else 1


if __name__ == "__main__":
    raise SystemExit(main())
