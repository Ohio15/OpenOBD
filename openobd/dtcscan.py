"""
dtcscan.py -- headless per-module DTC read over the OBDX Pro GT.

Reads DTCs from every reachable module and prints them as text:
  * ECM and TCM via the OBD modes 03/07/0A (functional read_dtcs sees these too),
  * EBCM and BCM via UDS $19 02 -- the GMLAN chassis/body modules the functional
    read CANNOT reach, because they do not answer the OBD broadcast,
  * the SW-GMLAN modules (IPC, SDM, HVAC, radio, TCCM) have no HS diagnostic id
    on this path and are reported 'unreachable', never silently dropped.

Run it (Ron -- this opens the GT, a hardware action):

    D:/Projects/OpenOBD/.venv/Scripts/python.exe -m openobd.dtcscan

The GT must be free (close the OpenOBD GUI / Techline first) and out of binary
mode (replug USB + OBD for 10 s if it answers in J2534 binary). Exit codes:
0 ok, 3 interface/connect problem.

VERIFY ON THE TRUCK: the EBCM read uses UDS $19 by default. Your known C0035
(left-front wheel speed) is the ground truth -- if the EBCM line shows C0035, the
$19 path is correct for this module; if the EBCM NAKs $19, it uses a GM legacy
DTC service instead and we switch the EBCM/BCM path to that.
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
    if "dtcs" in codes:                       # UDS path (EBCM/BCM)
        return ", ".join(codes["dtcs"]) if codes["dtcs"] else "no codes"
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
