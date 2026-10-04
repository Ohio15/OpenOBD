"""
didscan.py -- READ-ONLY data-identifier discovery for one module over the GT's
pass-thru driver (ISO 15765, HS-GMLAN 500 kbps).

Purpose: show what a factory scan tool's data display would show, without the
factory tool. First pass (--discover) asks the module for every data
identifier in a range and records the ones it answers; later passes (--read
LABEL) re-read only that supported set, one file per vehicle state (e.g. the
transfer case knob in 2HI / AUTO / 4HI); --diff then lists the identifiers whose
bytes change between states -- for the TCCM, the shift position sensors.

Structurally read-only: the sender refuses every service except $22
ReadDataByIdentifier and $1A ReadDataByIdentifier(GM legacy). Nothing here can
clear, write, actuate or reprogram.

One pass-thru device session per process (see swcan.sweep_buses: the OBDX
driver can kill the process at close), and discovery hits are appended to a
JSONL file as they arrive, so an interrupted sweep keeps its progress and can be
resumed with --start.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Callable, Iterable, Optional

READ_ONLY_SIDS = frozenset({0x22, 0x1A})
NRC_PENDING = 0x78

#: modules this truck answers on, request -> response (vehnet evidence)
MODULE_IDS = {"7E0": "7E8", "7E2": "7EA", "7E3": "7EB", "7E4": "7EC"}


class NotReadOnly(ValueError):
    """A request that is not one of the read services was about to be sent."""


def _check_read_only(request: bytes) -> None:
    if not request or request[0] not in READ_ONLY_SIDS:
        raise NotReadOnly(f"refusing to send {request.hex().upper()}: didscan "
                          "sends $22/$1A reads only")


def classify(request: bytes, payload: bytes) -> Optional[tuple]:
    """Classify one reply payload (no CAN id) against the request.
    ('ok', data_bytes) for a positive reply to THIS request, ('nrc', code) for
    a negative reply to this service, ('pending', None) for $78, else None."""
    if not payload:
        return None
    sid = request[0]
    if payload[0] == 0x7F and len(payload) >= 3 and payload[1] == sid:
        return ("pending", None) if payload[2] == NRC_PENDING else \
            ("nrc", payload[2])
    if payload[0] == (sid + 0x40) & 0xFF:
        echo = request[1:]
        if payload[1:1 + len(echo)] == echo:
            return ("ok", bytes(payload[1 + len(echo):]))
    return None


class Session:
    """One device session, one ISO 15765 channel to one module."""

    def __init__(self, j, req_id: int, resp_id: int, timeout_ms: int = 120):
        self.j, self.req, self.resp = j, req_id, resp_id
        self.timeout_ms = timeout_ms
        self.ch = None

    def __enter__(self):
        from . import j2534 as jt
        self.jt = jt
        self.j.open()
        try:
            self.ch = self.j.connect(jt.ISO15765, 500000)
            self.j.flow_control_filter(self.ch, self.req, self.resp)
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

    def query(self, request: bytes) -> Optional[tuple]:
        """Send one READ and wait for its classified reply (None = silence)."""
        _check_read_only(request)
        self.j.write(self.ch, self.req, request, timeout=self.timeout_ms)
        end = time.monotonic() + self.timeout_ms / 1000.0
        while True:
            for data in self.j.read(self.ch, timeout=self.timeout_ms // 4 or 1):
                if len(data) < 5:
                    continue
                if int.from_bytes(data[:4], "big") & 0x7FF != self.resp:
                    continue
                c = classify(request, data[4:])
                if c is None:
                    continue
                if c[0] == "pending":            # module asked for more time
                    end = time.monotonic() + 5.0
                    continue
                return c
            if time.monotonic() >= end:
                return None


def did_request(did: int) -> bytes:
    return bytes([0x22, (did >> 8) & 0xFF, did & 0xFF])


def ident_request(ident: int) -> bytes:
    return bytes([0x1A, ident & 0xFF])


def discover(sess: Session, start: int = 0x0000, end: int = 0xFFFF, *,
             idents: bool = True,
             on_hit: Callable[[str, bytes], None] = lambda k, v: None,
             on_progress: Callable[[int], None] = lambda did: None) -> dict:
    """Ask for every $1A ident (00-FF) and every $22 DID in [start, end].
    Returns {"hits": {key: hex}, "nrc": {code_hex: count}, "silent": n,
    "last": last DID asked}. Keys are '1A:XX' and '22:XXXX'."""
    out: dict = {"hits": {}, "nrc": {}, "silent": 0, "last": None}

    def handle(key: str, req: bytes) -> None:
        r = sess.query(req)
        if r is None:
            out["silent"] += 1
        elif r[0] == "ok":
            out["hits"][key] = r[1].hex().upper()
            on_hit(key, r[1])
        else:
            k = f"{r[1]:02X}"
            out["nrc"][k] = out["nrc"].get(k, 0) + 1

    if idents:
        for i in range(0x100):
            handle(f"1A:{i:02X}", ident_request(i))
    for did in range(start, end + 1):
        handle(f"22:{did:04X}", did_request(did))
        out["last"] = did
        if did % 0x100 == 0xFF:
            on_progress(did)
    return out


def read_keys(sess: Session, keys: Iterable[str]) -> dict:
    """Re-read a known supported set. Returns {key: hex or None}."""
    out = {}
    for key in keys:
        svc, ident = key.split(":")
        req = (did_request(int(ident, 16)) if svc == "22"
               else ident_request(int(ident, 16)))
        r = sess.query(req)
        out[key] = r[1].hex().upper() if r and r[0] == "ok" else None
    return out


def watch(sess: Session, keys: list, seconds: float,
          on_row: Callable[[float, dict], None],
          clock: Callable[[], float] = time.monotonic) -> int:
    """Re-read `keys` as fast as the module answers for `seconds`, handing
    each pass to on_row(t_since_start, {key: hex|None}). Read-only (the same
    $22/$1A sender). Returns the number of passes."""
    t0 = clock()
    n = 0
    while True:
        row = read_keys(sess, keys)
        n += 1
        t = clock() - t0
        on_row(t, row)
        if t >= seconds:
            return n


#: the TCCM identifiers that moved with the knob on 2026-10-04 (2HI/AUTO/4HI/
#: 4LO snapshots): 3114/3115 actuator position pair, 3142 knob request, 3140
#: status, plus the other position-correlated bytes.
#: Kept to six so a pass is ~0.2 s (the module answers ~30 reads/s).
TCCM_WATCH = ["22:3114", "22:3115", "22:3142", "22:3140", "22:312A",
              "22:3176"]


def diff(snapshots: dict) -> list:
    """{label: {key: hex}} -> [(key, {label: hex})] for every key whose value
    is not identical across all labels (a key missing in one counts)."""
    keys = sorted(set().union(*[set(s) for s in snapshots.values()]))
    rows = []
    for k in keys:
        vals = {lbl: snap.get(k) for lbl, snap in snapshots.items()}
        if len(set(vals.values())) > 1:
            rows.append((k, vals))
    return rows


def _supported_from_jsonl(path: str) -> list:
    keys = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("hit"):
                keys.append(rec["hit"])
    return sorted(set(keys))


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="python -m openobd.didscan",
                                 description=__doc__.split("\n\n")[1])
    ap.add_argument("--module", default="7E4", choices=sorted(MODULE_IDS))
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discover", action="store_true")
    mode.add_argument("--read", metavar="LABEL")
    mode.add_argument("--diff", nargs="+", metavar="FILE")
    mode.add_argument("--watch", metavar="LABEL",
                      help="record the TCCM watch set ~10x/s to a CSV")
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--start", default="0000")
    ap.add_argument("--end", default="FFFF")
    ap.add_argument("--no-idents", action="store_true")
    a = ap.parse_args(argv)
    mod = a.module.upper()
    found = f"didscan-{mod}-supported.jsonl"

    if a.diff:
        snaps = {}
        for p in a.diff:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            snaps[d.get("label", p)] = d["values"]
        rows = diff(snaps)
        print(f"{len(rows)} identifier(s) differ across {list(snaps)}:")
        for k, vals in rows:
            print(f"  {k}: " + "  ".join(f"{l}={v}" for l, v in vals.items()))
        return 0

    req, resp = int(mod, 16), int(MODULE_IDS[mod], 16)
    from . import j2534 as jt
    try:
        j = jt.J2534()
    except Exception as e:                                    # noqa: BLE001
        print(f"pass-thru driver not available: {e}")
        return 3
    try:
        with Session(j, req, resp) as sess:
            if a.watch:
                outp = f"didscan-{mod}-watch-{a.watch}.csv"
                keys = TCCM_WATCH
                print(f"recording {len(keys)} identifiers for {a.seconds:.0f} s "
                      f"-> {outp}. TURN THE KNOB NOW.")
                with open(outp, "w", encoding="utf-8") as fh:
                    fh.write("t_s," + ",".join(keys) + "\n")

                    def row(t, vals):
                        fh.write(f"{t:.3f}," + ",".join(
                            vals.get(k) or "" for k in keys) + "\n")
                        fh.flush()
                    n = watch(sess, keys, a.seconds, row)
                print(f"done: {n} passes ({n / max(a.seconds, 0.001):.1f}/s) "
                      f"-> {outp}")
            elif a.discover:
                start, end = int(a.start, 16), int(a.end, 16)
                print(f"discovering {mod}: $1A idents + $22 {start:04X}-{end:04X}"
                      " (read-only). Hits are saved as they arrive.")
                with open(found, "a", encoding="utf-8") as fh:
                    def hit(k, v):
                        fh.write(json.dumps({"hit": k, "value": v.hex().upper(),
                                             "t": time.time()}) + "\n")
                        fh.flush()
                        print(f"  {k} = {v.hex().upper()}")

                    def prog(did):
                        fh.write(json.dumps({"progress": f"{did:04X}"}) + "\n")
                        fh.flush()
                        if did % 0x1000 == 0xFFF:
                            print(f"  ... through {did:04X}")
                    res = discover(sess, start, end, idents=not a.no_idents,
                                   on_hit=hit, on_progress=prog)
                print(f"done: {len(res['hits'])} supported, NRCs {res['nrc']}, "
                      f"{res['silent']} silent. Saved to {found}.")
            else:
                try:
                    keys = _supported_from_jsonl(found)
                except FileNotFoundError:
                    print(f"run --discover first ({found} not found)")
                    return 2
                vals = read_keys(sess, keys)
                outp = f"didscan-{mod}-{a.read}.json"
                with open(outp, "w", encoding="utf-8") as fh:
                    json.dump({"module": mod, "label": a.read,
                               "t": time.time(), "values": vals}, fh, indent=1)
                ok = sum(1 for v in vals.values() if v is not None)
                print(f"read {ok}/{len(keys)} identifiers -> {outp}")
    except Exception as e:                                    # noqa: BLE001
        print(f"scan failed: {e}")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
