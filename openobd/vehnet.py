"""
vehnet — the vehicle network model behind the Diagnostics module map.

Describes the modules installed on the truck, which bus each lives on, and how
the tool reaches them (PC -> OBDX GT -> DLC -> bus -> module). The localizer
turns raw scan results into a per-segment verdict so the UI can point at WHERE
the communication pipeline broke, not just report "no data".

Pure stdlib — unit-tested without Qt or hardware.

Network reference: 2010 Silverado 1500 (GMT900).
  HS-GMLAN (2-wire CAN, 500 kb/s, DLC pins 6/14): powertrain + chassis.
  SW-GMLAN (single-wire CAN, 33.3 kb/s, DLC pin 1): body/comfort.
The ELM327-compatible HS-CAN path of the OBDX GT cannot open the single-wire
bus, so SW modules are UNREACHABLE (amber), which is not the same as FAILED.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

HS = "HS-GMLAN (500k)"
SW = "SW-GMLAN (33.3k)"


@dataclass(frozen=True)
class ModuleDef:
    key: str
    name: str
    bus: str
    req_id: Optional[str]     # 11-bit physical request CAN id (hex) or None
    resp_id: Optional[str]    # expected response id (hex) or None
    obd_responder: bool       # answers the functional OBD 0100 broadcast
    role: str                 # one-line description for the details panel


# Diagnostic addressing MEASURED on this truck (not the generic convention —
# the old table guessed TCM 7E1/7E9 and EBCM 241/641, both wrong here):
#   * HS-CAN sniff 2026-08-22 (e38flash captures/hpt-entry-run2-
#     20260822T161920Z.jsonl) shows diagnostic responses on 7E8, 7EA, 7EB,
#     7EC, 641, 643 and 64D. ISO 15765 powertrain ids answer at request + 8;
#     GMLAN chassis/body ids answer USDT at request + 0x400.
#   * truck-mcp (MX+ on the same truck) confirms 7EA = 'TCM-TransmisCtrl'
#     (7E2 -> 7EA), 243 -> 643 = EBCM, 241 = BCM.
#   * HP Tuners reached the BCM at 0x541 UUDT and the EBCM at 0x543 UUDT,
#     i.e. both sit on HS-GMLAN. The BCM is ALSO the gateway to SW-GMLAN.
# 7EB, 7EC and 64D answered too but their modules are not identified; they are
# deliberately left out rather than given invented names.
# SW modules carry no HS ids — unreachable from this path.
MODULES: list[ModuleDef] = [
    ModuleDef("ecm",  "ECM (E38)",        HS, "7E0", "7E8", True,
              "Engine control — fueling, spark, DoD/AFM"),
    ModuleDef("tcm",  "TCM (T43)",        HS, "7E2", "7EA", True,
              "6L80 transmission — shifts, TCC, line pressure"),
    ModuleDef("ebcm", "EBCM (ABS)",       HS, "243", "643", False,
              "Brake control — ABS, traction, StabiliTrak"),
    ModuleDef("bcm",  "BCM",              HS, "241", "641", False,
              "Body control — power, lighting; gateway between HS-GMLAN "
              "and the SW-GMLAN body bus"),
    ModuleDef("ipc",  "IPC (cluster)",    SW, None, None, False,
              "Instrument cluster — gauges, DIC, chimes"),
    ModuleDef("sdm",  "SDM (airbag)",     SW, None, None, False,
              "Inflatable restraint sensing and deployment"),
    ModuleDef("hvac", "HVAC",             SW, None, None, False,
              "Climate control head and actuators"),
    ModuleDef("radio", "Radio",           SW, None, None, False,
              "Entertainment head unit"),
    ModuleDef("tccm", "TCCM (4WD)",       SW, None, None, False,
              "Transfer case shift control"),
]


class Status(Enum):
    UNKNOWN = "unknown"          # not scanned yet
    OK = "responding"            # answered a request this scan
    SILENT = "no response"       # VERIFIED request (header + filter confirmed)
                                 # went out and nothing answered
    NOT_EXAMINED = "not examined"  # scan tried, but the GT refused the
                                 # addressing setup — silence proves nothing
    UNREACHABLE = "unreachable"  # current tool path cannot address this bus


class SegStatus(Enum):
    UNKNOWN = "unknown"
    OK = "ok"
    FAILED = "failed"
    UNREACHABLE = "unreachable"


@dataclass
class ScanResult:
    """Raw facts from one scan pass, in pipeline order."""
    port_open: bool                      # PC -> OBDX GT serial link
    interface_alive: bool                # GT positively identified as ELM/OBDX
    dlc_volts: Optional[float]           # parsed ATRV (None = UNREADABLE,
                                         # which is not 'no voltage')
    hs_responders: set[str]              # response CAN ids seen on 0100 bcast
                                         # (positive evidence only)
    # module key -> True answered / False verified silence / None not
    # examined (header or filter could not be confirmed). Absent = not tried.
    pinged: dict[str, Optional[bool]]
    binary_mode: bool = False            # GT answered text commands in its
                                         # J2534 binary mode
    interface_reply: str = ""            # what the GT said, for the notes
    dlc_reply: str = ""                  # raw ATRV reply, for the notes


@dataclass
class Verdict:
    segments: dict[str, SegStatus]       # pc_gt / gt_dlc / dlc_hs / dlc_sw
    modules: dict[str, Status]           # module key -> status
    failure_point: Optional[str]         # first broken segment, pipeline order
    notes: list[str]


def localize(scan: ScanResult) -> Verdict:
    """Walk the pipeline in order; the first dead stage explains everything
    downstream (downstream stages stay UNKNOWN, not FAILED).

    Evidence rules (unknown is never clean, and silence from an unverified
    request is unknown, not a fault):
      * a module is OK on positive evidence only (broadcast line or ping reply);
      * a module is SILENT only when a physical ping with a CONFIRMED header
        and receive filter got nothing back;
      * absence from the functional broadcast proves nothing on its own;
      * an unparseable ATRV is 'voltage unreadable', never 'no voltage'."""
    segs = {"pc_gt": SegStatus.UNKNOWN, "gt_dlc": SegStatus.UNKNOWN,
            "dlc_hs": SegStatus.UNKNOWN, "dlc_sw": SegStatus.UNREACHABLE}
    mods = {m.key: Status.UNKNOWN for m in MODULES}
    for m in MODULES:
        if m.bus == SW:
            mods[m.key] = Status.UNREACHABLE
    notes: list[str] = []
    failure: Optional[str] = None

    # Stage 1: PC -> GT (serial port + interface identified as ELM/OBDX text)
    if scan.port_open and scan.binary_mode:
        segs["pc_gt"] = SegStatus.FAILED
        notes.append("OBDX GT is in its binary (J2534) mode — a J2534 tool "
                     "used it last. Unplug the GT from USB AND the OBD port "
                     "for 10 s, then rescan.")
        if scan.interface_reply:
            notes.append(f"GT reply to a text command: {scan.interface_reply}")
        return Verdict(segs, mods, "pc_gt:binary_mode", notes)
    if not scan.port_open or not scan.interface_alive:
        segs["pc_gt"] = SegStatus.FAILED
        notes.append("OBDX GT not reachable — check USB cable / COM port.")
        if scan.port_open and scan.interface_reply:
            notes.append("The port opened but the reply did not identify an "
                         f"ELM/OBDX interface: {scan.interface_reply}")
        return Verdict(segs, mods, "pc_gt", notes)
    segs["pc_gt"] = SegStatus.OK

    # Stage 2: GT -> DLC (battery voltage present at pin 16). Only a PARSED
    # reading can fail this stage; an unreadable one leaves it unknown and the
    # scan continues — the bus stages below still produce real evidence.
    if scan.dlc_volts is None:
        notes.append("DLC voltage unreadable — the GT's ATRV reply did not "
                     f"parse ({scan.dlc_reply!r}). This is not a power "
                     "fault finding.")
    elif scan.dlc_volts < 6.0:
        segs["gt_dlc"] = SegStatus.FAILED
        notes.append(f"Low battery voltage at the DLC ({scan.dlc_volts:.1f} V)"
                     " — is the GT plugged into the truck, and is the DLC "
                     "pin 16 fused circuit alive?")
        return Verdict(segs, mods, "gt_dlc", notes)
    else:
        segs["gt_dlc"] = SegStatus.OK

    # Stage 3: DLC -> HS bus. Positive evidence: a broadcast line or any
    # answered ping. Failure needs at least one VERIFIED silent ping.
    hs_keys = [m.key for m in MODULES if m.bus == HS]
    responders = {r.upper() for r in scan.hs_responders}
    any_hs = bool(responders) or any(
        scan.pinged.get(k) is True for k in hs_keys)
    verified_silent = [k for k in hs_keys if scan.pinged.get(k) is False]
    not_examined = [k for k in hs_keys
                    if k in scan.pinged and scan.pinged[k] is None]
    if not any_hs:
        for k in not_examined:
            mods[k] = Status.NOT_EXAMINED
        if verified_silent:
            segs["dlc_hs"] = SegStatus.FAILED
            notes.append("HS-GMLAN completely silent — ignition off, bus "
                         "wiring (DLC pins 6/14), or total bus fault.")
            return Verdict(segs, mods, "dlc_hs", notes)
        notes.append("HS-GMLAN not examined — the GT refused the addressing "
                     "commands (ATSH/ATCRA), so no request could be verified."
                     " Rescan; if it repeats, power-cycle the GT.")
        return Verdict(segs, mods, None, notes)
    segs["dlc_hs"] = SegStatus.OK

    # Stage 4: individual HS modules
    for m in MODULES:
        if m.bus != HS:
            continue
        seen = bool(m.resp_id) and m.resp_id.upper() in responders
        ping = scan.pinged.get(m.key, "absent")
        if seen or ping is True:
            mods[m.key] = Status.OK
        elif ping is False:
            mods[m.key] = Status.SILENT
            if failure is None:
                failure = f"module:{m.key}"
            notes.append(f"{m.name} did not answer a verified request on "
                         f"{m.bus} ({m.req_id}->{m.resp_id}) — module, "
                         f"connector, or its bus stub.")
        elif ping is None:
            mods[m.key] = Status.NOT_EXAMINED
            notes.append(f"{m.name} not examined — the GT refused the header/"
                         f"filter for {m.req_id}->{m.resp_id}; its silence "
                         "would prove nothing. Rescan.")
        else:
            mods[m.key] = Status.UNKNOWN

    notes.append("SW-GMLAN (IPC/body) is not reachable over the ELM HS-CAN "
                 "path — needs the GT's single-wire mode (future). The BCM "
                 "gateways it, and the BCM itself is on HS-GMLAN.")
    return Verdict(segs, mods, failure, notes)


def scan_pipeline(gt) -> ScanResult:
    """Run one full module-map scan against a connected GT (duck-typed:
    scan_network() + ping_module()). Every HS module not already proven by
    the broadcast gets a physical ping — broadcast absence is never taken as
    silence. Pings are skipped entirely when the interface is not a verified
    ELM/OBDX text link (binary mode, unidentified reply)."""
    facts = gt.scan_network()
    pinged: dict[str, Optional[bool]] = {}
    if facts.get("interface_alive"):
        seen = {r.upper() for r in facts.get("hs_responders", set())}
        for m in MODULES:
            if m.bus != HS or not m.req_id:
                continue
            if m.resp_id.upper() in seen:
                pinged[m.key] = True
            else:
                pinged[m.key] = gt.ping_module(m.req_id, m.resp_id)
    return ScanResult(
        port_open=True,
        interface_alive=bool(facts.get("interface_alive")),
        dlc_volts=facts.get("dlc_volts"),
        hs_responders=set(facts.get("hs_responders", set())),
        pinged=pinged,
        binary_mode=bool(facts.get("binary_mode")),
        interface_reply=facts.get("interface_reply", ""),
        dlc_reply=facts.get("dlc_reply", ""))


def _module(key: str) -> Optional[ModuleDef]:
    for m in MODULES:
        if m.key == key:
            return m
    return None
