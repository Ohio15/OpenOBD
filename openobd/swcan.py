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
from typing import Callable, Optional

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


def _sweep_channel(j, jt, bus: str, capture_s: float, per_id_s: float) -> dict:
    """One bus on an ALREADY OPEN device: connect a channel, sweep, disconnect.
    Never opens or closes the device (see sweep_buses for why)."""
    if bus not in ("sw", "hs"):
        raise ValueError(f"unknown bus {bus!r}")
    proto = jt.SW_CAN_PS if bus == "sw" else jt.CAN
    baud = jt.SW_CAN_BAUD if bus == "sw" else 500000
    out: dict = {"examined": False, "error": None, "responders": {},
                 "negatives": {}, "vbatt": None, "frames": 0}
    ch: Optional[int] = None
    frames: list[bytes] = []
    try:
        ch = j.connect(proto, baud)
        if bus == "sw":                  # _PS protocols need their pins set
            j.set_config(ch, [(jt.J1962_PINS, jt.SW_CAN_PINS)])
        j.pass_filter(ch, proto, 0x500, 0x700)              # UUDT reports
        j.pass_filter(ch, proto, 0x600, 0x700)              # USDT refusals

        def collect(seconds: float) -> None:
            # read at least once, so a reply already queued is never dropped
            end = time.monotonic() + seconds
            while True:
                frames.extend(j.read(ch, timeout=100, max_msgs=32))
                if time.monotonic() >= end:
                    break

        j.write(ch, FUNCTIONAL_ID, A9_FUNCTIONAL, proto=proto, txflags=0)
        collect(capture_s)
        heard = {int.from_bytes(f[:4], "big") & 0x7FF for f in frames
                 if len(f) >= 5}
        for req in PHYSICAL_IDS:
            if (req + 0x300) in heard or (req + 0x400) in heard:
                continue
            j.write(ch, req, A9_PHYSICAL, proto=proto, txflags=0)
            collect(per_id_s)
    except Exception as e:                                    # noqa: BLE001
        out["error"] = f"{bus.upper()}-CAN sweep failed: {e}"
    finally:
        if ch is not None:
            try:
                j.disconnect(ch)
            except Exception:                                 # noqa: BLE001
                pass
    out["frames"] = len(frames)
    s = summarize(frames)
    out["responders"], out["negatives"] = s["responders"], s["negatives"]
    out["examined"] = bool(out["responders"] or out["negatives"])
    if not out["examined"] and not out["error"]:
        out["error"] = (f"no {bus.upper()}-CAN node answered $A9 ({len(frames)} "
                        "frames captured) -- bus asleep, wrong pins, or the "
                        "driver lacks the protocol")
    return out


def sweep_buses(buses, on_result: Callable[[str, dict], None], j=None, *,
                capture_s: float = 2.0, per_id_s: float = 0.3) -> Optional[str]:
    """Sweep several buses in ONE device session, handing each result to
    on_result as soon as it exists (so the caller can print and flush it).

    One session per process, on purpose: the OBDX GT driver is a .NET DLL whose
    background serial thread can read the port after PassThruClose and kill the
    whole process with an unhandled InvalidOperationException ('The port is
    closed') -- observed on the truck 2026-10-04 with one open/close per bus.
    So the device is opened once, every bus gets its own channel, and the close
    is the last thing that happens, after every result has been delivered.
    Returns an error string if the device could not be opened, else None."""
    from . import j2534 as jt
    for b in buses:
        if b not in ("sw", "hs"):
            raise ValueError(f"unknown bus {b!r}")
    try:
        if j is None:
            j = jt.J2534()
        j.open()
    except Exception as e:                                    # noqa: BLE001
        return f"pass-thru open failed: {e}"
    try:
        vbatt = None
        try:
            vbatt = j.read_vbatt()
        except Exception:                                     # noqa: BLE001
            pass
        for b in buses:
            res = _sweep_channel(j, jt, b, capture_s, per_id_s)
            res["vbatt"] = vbatt
            on_result(b, res)
    finally:
        try:
            j.close()
        except Exception:                                     # noqa: BLE001
            pass
    return None


def sweep(j=None, *, bus: str = "sw", capture_s: float = 2.0,
          per_id_s: float = 0.3) -> dict:
    """Run the $A9 sweep on one bus in its own device session: bus "sw" is
    single-wire GMLAN (33.3 kbps, pin 1); bus "hs" is HS-GMLAN (500 kbps, the
    default CAN pins 6/14). Returns {"examined", "error", "responders",
    "negatives", "vbatt", "frames"}; silence is NOT EXAMINED, never 'clean'.
    Use sweep_buses for more than one bus in a process."""
    if bus not in ("sw", "hs"):
        raise ValueError(f"unknown bus {bus!r}")
    got: dict = {}
    err = sweep_buses([bus], lambda b, r: got.update(r), j,
                      capture_s=capture_s, per_id_s=per_id_s)
    if err:
        return {"examined": False, "error": err, "responders": {},
                "negatives": {}, "vbatt": None, "frames": 0}
    return got
