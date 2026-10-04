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

from .gt import ObdxGt, describe_no_gt
from . import vehnet


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


def main(argv=None) -> int:
    port = ObdxGt.autodetect()
    if not port:
        print(describe_no_gt())
        return 3
    gt = ObdxGt(port)
    try:
        gt.open()
    except RuntimeError as e:
        print(f"GT connect failed: {e}")
        return 3
    try:
        print(f"OBDX Pro GT on {port} -- per-module DTC scan\n")
        res = vehnet.scan_all_module_dtcs(gt)
        width = max(len(i["name"]) for i in res.values())
        for info in res.values():
            print(f"  {info['name']:<{width}}  {_fmt(info)}")
    finally:
        gt.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
