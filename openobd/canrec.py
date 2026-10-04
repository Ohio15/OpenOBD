"""
canrec.py -- LISTEN-ONLY recorder of HS-GMLAN diagnostic traffic through the
GT's pass-thru driver, for capturing what ANOTHER scan tool sends.

Use: the GT and another tool (e.g. an Autel) share the DLC on a Y-splitter.
Start this, run the other tool's function (the TCCM Range Actuator Learn),
stop with Ctrl+C or let the duration run out. Every diagnostic frame on the
bus -- the other tool's requests AND the modules' answers -- lands in a JSONL
file with a timestamp, so OpenOBD can later replicate the routine from
evidence instead of guessing an actuation command.

Never transmits: this module has no code path that calls write(). The
pass-thru channel is raw CAN at 500 kbps with pass filters on the GMLAN
diagnostic id ranges only (the bus carries thousands of non-diagnostic frames
a second, which would swamp the GT's USB link):
  0x7E0-0x7EF  powertrain-block requests/responses (ECM, TCM, FPCM, TCCM 7E4/7EC)
  0x7DF        OBD functional
  0x101        GMLAN AllNodes functional
  0x240-0x24F  chassis/body physical requests
  0x540-0x54F, 0x5E0-0x5EF  UUDT reports
  0x640-0x64F  chassis/body USDT responses
One device session per run (the OBDX driver can kill the process at close).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Callable, Optional

#: (pattern, mask) pass filters, 11-bit ids -- at most 10 per channel.
DIAG_FILTERS = [(0x7E0, 0x7F0), (0x7DF, 0x7FF), (0x101, 0x7FF),
                (0x240, 0x7F0), (0x540, 0x7F0), (0x5E0, 0x7F0),
                (0x640, 0x7F0)]


def frame_record(t: float, data: bytes) -> Optional[dict]:
    """One raw pass-thru frame (4-byte id + up to 8 data bytes) -> record."""
    if len(data) < 4:
        return None
    return {"t": round(t, 4),
            "id": f"{int.from_bytes(data[:4], 'big') & 0x1FFFFFFF:03X}",
            "data": data[4:].hex().upper()}


def record(j, seconds: float, on_frame: Callable[[dict], None],
           clock: Callable[[], float] = time.monotonic,
           should_stop: Callable[[], bool] = lambda: False) -> dict:
    """Listen for `seconds` (or until should_stop()). Never writes to the bus.
    Returns {"frames": n, "error": str|None}."""
    from . import j2534 as jt
    out = {"frames": 0, "error": None}
    try:
        j.open()
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"pass-thru open failed: {e}"
        return out
    ch = None
    try:
        ch = j.connect(jt.CAN, 500000)
        for patt, mask in DIAG_FILTERS:
            j.pass_filter(ch, jt.CAN, patt, mask)
        t0 = clock()
        while True:
            for data in j.read(ch, timeout=50, max_msgs=64):
                rec = frame_record(clock() - t0, data)
                if rec:
                    out["frames"] += 1
                    on_frame(rec)
            if clock() - t0 >= seconds or should_stop():
                break
    except KeyboardInterrupt:
        out["error"] = None                     # Ctrl+C is the normal stop
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"recording failed: {e}"
    finally:
        if ch is not None:
            try:
                j.disconnect(ch)
            except Exception:                                 # noqa: BLE001
                pass
        try:
            j.close()
        except Exception:                                     # noqa: BLE001
            pass
    return out


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="python -m openobd.canrec")
    ap.add_argument("label")
    ap.add_argument("--seconds", type=float, default=600.0)
    a = ap.parse_args(argv)
    outp = f"canrec-{a.label}.jsonl"
    from . import j2534 as jt
    try:
        j = jt.J2534()
    except Exception as e:                                    # noqa: BLE001
        print(f"pass-thru driver not available: {e}")
        return 3
    print(f"LISTENING (never transmits) for up to {a.seconds:.0f} s -> {outp}")
    print("Run the function on the other scan tool now. Ctrl+C here when done.")
    counts: dict = {}
    with open(outp, "w", encoding="utf-8") as fh:
        def on_frame(rec):
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            counts[rec["id"]] = counts.get(rec["id"], 0) + 1
            n = sum(counts.values())
            if n % 200 == 0:
                print(f"  {n} frames so far")
        res = record(j, a.seconds, on_frame)
    print(f"stopped: {res['frames']} frames -> {outp}"
          + (f"  [{res['error']}]" if res["error"] else ""))
    for cid, n in sorted(counts.items()):
        print(f"  0x{cid}: {n}")
    return 0 if not res["error"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
