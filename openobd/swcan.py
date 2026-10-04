"""
swcan.py -- read-only GM $A9 DTC sweep of the SINGLE-WIRE GMLAN body bus
(SW-CAN, 33.3 kbps, J1962 pin 1) through the OBDX Pro GT's pass-thru driver.

Why pass-thru: the GT's ELM327 text mode has no single-wire protocol and the GT
rejects the STN extensions (STP/STCSWM) that add one, so the ELM path cannot
select this bus. The GT's pass-thru driver registers SW_CAN_PS = 1 and
SW_ISO15765_PS = 1 (PassThruSupport.04.04 registry, checked 2026-10-04).

What it sends -- $A9 81 (read DTCs by status) only, nothing that clears or
writes:
  1. functional: AllNodes 0x101, FE-framed (the framing e38flash verified on
     this truck's HS bus), so every SW node answers on its own id;
  2. physical: 0x241..0x25F one at a time, skipping ids already heard, for any
     node that ignores the functional request (truck-mcp found SW nodes
     243/245/24D/251 this way).
Reports arrive UUDT on request+0x300 (0x5xx), refusals USDT on +0x400 (0x6xx).

COST: after a pass-thru session the GT stays in binary mode until it is
unplugged (USB and OBD, 10 s). The CLI runs this step LAST and says so.
"""
from __future__ import annotations

import time
from typing import Optional

from .gt import parse_a9_report

FUNCTIONAL_ID = 0x101
A9_FUNCTIONAL = bytes.fromhex("FE03A981FF555555")
A9_PHYSICAL = bytes.fromhex("03A981FF55555555")
PHYSICAL_IDS = tuple(range(0x241, 0x260))


def frames_to_report_text(frames: list[bytes]) -> str:
    """Raw pass-thru frames (4-byte CAN id + data) -> the space-separated
    '<3-hex id><data hex>' tokens parse_a9_report already decodes."""
    toks = []
    for f in frames:
        if len(f) < 5:
            continue
        cid = int.from_bytes(f[:4], "big") & 0x7FF
        toks.append(f"{cid:03X}{f[4:].hex().upper()}")
    return " ".join(toks)


def summarize(frames: list[bytes]) -> dict:
    """Group captured frames into {responders: {uudt: report}, negatives:
    {usdt: nrc}}. Pure; the tests drive it with recorded frame shapes."""
    text = frames_to_report_text(frames)
    out: dict = {"responders": {}, "negatives": {}}
    for tok in text.split():
        cid = tok[:3]
        if cid[0] == "5" and tok[3:5] == "81" and cid not in out["responders"]:
            out["responders"][cid] = None
        elif cid[0] == "6" and len(tok) >= 11 and tok[5:9] == "7FA9":
            out["negatives"][cid] = tok[9:11]
    for cid in list(out["responders"]):
        out["responders"][cid] = parse_a9_report(text, uudt_id=cid)
    return out


def sweep(j=None, *, capture_s: float = 2.0, per_id_s: float = 0.3) -> dict:
    """Run the SW-CAN $A9 sweep. `j` is an openobd pass-thru client (injected
    by tests; built from the registered OBDX driver otherwise). Returns
    {"examined", "error", "responders", "negatives", "vbatt", "frames"};
    silence is NOT EXAMINED, never 'clean'."""
    from . import j2534 as jt
    out: dict = {"examined": False, "error": None, "responders": {},
                 "negatives": {}, "vbatt": None, "frames": 0}
    try:
        if j is None:
            j = jt.J2534()
        j.open()
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"pass-thru open failed: {e}"
        return out
    ch: Optional[int] = None
    frames: list[bytes] = []
    try:
        try:
            out["vbatt"] = j.read_vbatt()
        except Exception:                                     # noqa: BLE001
            pass
        ch = j.connect(jt.SW_CAN_PS, jt.SW_CAN_BAUD)
        j.set_config(ch, [(jt.J1962_PINS, jt.SW_CAN_PINS)])
        j.pass_filter(ch, jt.SW_CAN_PS, 0x500, 0x700)       # UUDT reports
        j.pass_filter(ch, jt.SW_CAN_PS, 0x600, 0x700)       # USDT refusals

        def collect(seconds: float) -> None:
            # read at least once, so a reply already queued is never dropped
            end = time.monotonic() + seconds
            while True:
                frames.extend(j.read(ch, timeout=100, max_msgs=32))
                if time.monotonic() >= end:
                    break

        j.write(ch, FUNCTIONAL_ID, A9_FUNCTIONAL, proto=jt.SW_CAN_PS, txflags=0)
        collect(capture_s)
        heard = {int.from_bytes(f[:4], "big") & 0x7FF for f in frames
                 if len(f) >= 5}
        for req in PHYSICAL_IDS:
            if (req + 0x300) in heard or (req + 0x400) in heard:
                continue
            j.write(ch, req, A9_PHYSICAL, proto=jt.SW_CAN_PS, txflags=0)
            collect(per_id_s)
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"SW-CAN sweep failed: {e}"
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
    out["frames"] = len(frames)
    s = summarize(frames)
    out["responders"], out["negatives"] = s["responders"], s["negatives"]
    out["examined"] = bool(out["responders"] or out["negatives"])
    if not out["examined"] and not out["error"]:
        out["error"] = (f"no SW-CAN node answered $A9 ({len(frames)} frames "
                        "captured) -- bus asleep, wrong pin, or no SW driver")
    return out
