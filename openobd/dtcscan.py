"""
dtcscan.py -- headless per-module DTC read over the OBDX Pro GT.

Reads DTCs from every reachable module and prints them as text:
  * ECM and TCM via the OBD modes 03/07/0A (functional read_dtcs sees these too),
  * EBCM and BCM via GMLAN $A9 81 -- the chassis/body modules the functional
    read CANNOT reach, because they do not answer the OBD broadcast. $A9 returns
    the module's whole supported-DTC table; only entries whose status byte marks
    a fault are printed (status shown in brackets), the rest are counted,
  * the SW-GMLAN modules (IPC, SDM, HVAC, radio, TCCM) have no HS diagnostic id
    on this path and are reported 'unreachable', never silently dropped.

Run it (Ron -- this opens the GT, a hardware action):

    D:/Projects/OpenOBD/.venv/Scripts/python.exe -m openobd.dtcscan

The GT must be free (close the OpenOBD GUI / Techline first) and out of binary
mode (replug USB + OBD for 10 s if it answers in J2534 binary). Exit codes:
0 ok, 3 interface/connect problem.

VERIFY ON THE TRUCK: the known LF wheel-speed fault (C0035) read status D3
when it was live (truck-mcp, 2026-08-03). A healthy or not-yet-retested entry
reads a housekeeping status (01/19/21/25) and is counted, not printed.
"""
from __future__ import annotations

import sys
from typing import Optional

from .gt import GtBinaryMode, ObdxGt, describe_no_gt
from . import swcan, vehnet


def _fmt(info: dict) -> str:
    if "unreachable" in info:
        return f"-- {info['unreachable']}"
    r = info["result"]
    if not r["examined"]:
        return f"NOT EXAMINED -- {r.get('error') or 'no answer'}"
    codes = r["codes"]
    if "dtcs" in codes:                       # GMLAN $A9 path (EBCM/BCM)
        faults = codes["dtcs"]
        status = {}
        for code, _sym, st in r.get("records", []):
            if code in faults:
                status.setdefault(code, []).append(st)
        body = (", ".join(f"{c} [{'/'.join(status[c])}]" if c in status else c
                          for c in faults)
                if faults else "no fault codes")
        table = codes.get("table", [])
        tail = (f"  ({len(table)} supported-DTC table entries read; "
                f"{len(table) - len(faults)} healthy)") if table else ""
        err = f"  [{r['error']}]" if r.get("error") else ""
        return body + tail + err
    parts = []                                # OBD path (ECM/TCM)
    for k in ("stored", "pending", "permanent"):
        if k in codes:
            parts.append(f"{k}: {', '.join(codes[k]) if codes[k] else 'none'}")
    tail = f"  [{r['error']}]" if r.get("error") else ""
    return (" | ".join(parts) if parts else "no usable answer") + tail


# HS-GMLAN UUDT ids identified on this truck (vehnet.MODULES evidence).
_KNOWN_UUDT = {"541": "BCM", "543": "EBCM (ABS)"}


_HS_HEAD = "Functional $A9 sweep (all HS-GMLAN nodes, 0x101):"
_SW_HEAD = ("SW-GMLAN body bus $A9 sweep (single-wire, pin 1, via pass-thru; "
            "functional 0x101 then 0x241-0x25F):")


def _fmt_sweep(sw: dict, head: str = _HS_HEAD,
               known: Optional[dict] = None) -> str:
    """$A9 sweep -> text. Every id that answered is listed; an id outside the
    identified set is flagged, with its fault codes if any."""
    known = _KNOWN_UUDT if known is None else known
    if not sw.get("examined"):
        return f"{head}\n  NOT EXAMINED -- {sw.get('error') or 'no answer'}"
    lines = [head]
    for cid, rep in sorted(sw["responders"].items()):
        who = known.get(cid, "UNACCOUNTED -- not an identified module")
        faults = ", ".join(f"{c} [{st}]" for c, _s, st in rep["records"]
                           if c in rep["codes"]) or "no fault codes"
        lines.append(f"  0x{cid} {who}: {faults}  "
                     f"({len(rep['table'])} table entries)")
    for cid, nrc in sorted(sw["negatives"].items()):
        lines.append(f"  0x{cid} refused $A9 (NRC {nrc}) -- a module is there")
    if sw.get("error"):
        lines.append(f"  [{sw['error']}]")
    return "\n".join(lines)


def main(argv=None) -> int:
    port = ObdxGt.autodetect()
    if not port:
        print(describe_no_gt())
        return 3
    gt = ObdxGt(port)
    binary = False
    try:
        gt.open()
    except GtBinaryMode:
        # The GT is already in pass-thru (binary) mode. The text-mode reads
        # (OBD modes for ECM/TCM, per-address $A9) cannot run, but both $A9
        # sweeps can — through the pass-thru driver, which binary mode IS.
        binary = True
    except RuntimeError as e:
        print(f"GT connect failed: {e}")
        return 3
    if binary:
        print(f"OBDX Pro GT on {port} is in pass-thru (binary) mode -- the "
              "text-mode reads (ECM/TCM OBD codes, per-module lines) are "
              "skipped. Unplug USB + OBD for 10 s to get them next time.\n")
        print(_fmt_sweep(swcan.sweep(bus="hs"),
                         head=_HS_HEAD.replace("0x101", "0x101, via pass-thru")))
    else:
        try:
            print(f"OBDX Pro GT on {port} -- per-module DTC scan\n")
            res = vehnet.scan_all_module_dtcs(gt)
            width = max(len(i["name"]) for i in res.values())
            for info in res.values():
                print(f"  {info['name']:<{width}}  {_fmt(info)}")
            print()
            print(_fmt_sweep(gt.sweep_gmlan_dtcs()))
        finally:
            gt.close()
    # LAST, after the serial port is released: the single-wire body bus is only
    # reachable through the GT's pass-thru driver, which leaves the GT in binary
    # mode until it is unplugged. No SW module is identified yet, so every
    # responder prints by address.
    print()
    sw = swcan.sweep(bus="sw")
    if sw.get("vbatt") is not None:
        print(f"(battery at the OBD port: {sw['vbatt']:.2f} V)")
    print(_fmt_sweep(sw, head=_SW_HEAD, known={}))
    print("\nNOTE: the GT is now in pass-thru (binary) mode. Unplug its USB and "
          "OBD plug for 10 s before the next full scan (this scan still works "
          "without it, but skips the text-mode reads).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
