"""
gt.py -- OBDX Pro GT live transport (ELM327 v2.1 compatible) + OBD-II polling.

The OBDX Pro GT enumerates as a USB CDC virtual COM port (STM32, VID 0483 /
PID 5740) and speaks the ELM327 command set. This module is the CLI/transport
seam the dashboard's GtDataSource wraps:

    open() / close() / command(cmd) / request_raw(mode_pid)

plus poll_timed() which runs one poll cycle of the pollplan schedule: the due
supported mode-01 PIDs, up to six per request, decoded into OpenOBD canonical
channel keys (see logbin.CANONICAL) with GAUGE_SPECS units (vss in mph, temps
in F, fuel pressure in psi), each carrying the send and reply times of its own
request; poll_once() is a full sweep (every tier) as a plain {key: value}
dict.

Read-only: only ELM AT config, OBD mode-01 data requests, the DTC reads and
mode-09 vehicle information requests (VIN / CALID / CVN,
read_vehicle_identity) are issued. No ECU writes.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, NamedTuple, Optional

from . import pollplan
from .transport import Reading

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pyserial optional until the GT path is used
    serial = None
    list_ports = None


def _u16(b):
    return b[0] * 256 + b[1]


def _temp_f(c):
    return c * 9 / 5 + 32


class PidDef(NamedTuple):
    """One Mode 01 PID: its canonical key, the data bytes its reply carries
    (needed to walk a batched reply, where the ECM may answer the PIDs in any
    order), and the decoder from those bytes to gauge units."""
    key: str
    nbytes: int
    decode: Callable[[list], float]


# PID byte (mode 01) -> PidDef. Units are GAUGE_SPECS units. Lengths per SAE
# J1979 (cross-checked against truck-mcp obd.py PIDS). PIDs 03, 08, 09, 14, 15,
# 18, 19, 3C, 3D, 43, 45, 47 and 4A were added in v0.33.0; all are in this
# ECM's supported-PID bitmap from the 2026-10-25 drive's probe.
PID_TABLE: dict[str, PidDef] = {
    # Fuel system status: byte0 is a bitfield (1 open loop cold, 2 closed
    # loop, 4 open loop load/decel, 8 open loop fault, 16 closed loop with a
    # fault). Recorded as the raw code; byte1 (system 2) is not used here.
    "03": PidDef("fuel_system",  2, lambda b: float(b[0])),
    "04": PidDef("load",         1, lambda b: b[0] * 100.0 / 255.0),        # %
    "05": PidDef("ect",          1, lambda b: _temp_f(b[0] - 40)),          # F
    "06": PidDef("stft",         1, lambda b: (b[0] - 128) * 100.0 / 128),  # %
    "07": PidDef("ltft",         1, lambda b: (b[0] - 128) * 100.0 / 128),  # %
    "08": PidDef("stft_b2",      1, lambda b: (b[0] - 128) * 100.0 / 128),  # %
    "09": PidDef("ltft_b2",      1, lambda b: (b[0] - 128) * 100.0 / 128),  # %
    "0A": PidDef("fuel_press",   1, lambda b: b[0] * 3 * 0.1450377),        # kPa->psi
    "0B": PidDef("map",          1, lambda b: float(b[0])),                 # kPa
    "0C": PidDef("rpm",          2, lambda b: _u16(b) / 4.0),               # rpm
    "0D": PidDef("vss",          1, lambda b: b[0] * 0.6213712),            # km/h->mph
    "0E": PidDef("spark",        1, lambda b: b[0] / 2.0 - 64.0),           # deg
    "0F": PidDef("iat",          1, lambda b: _temp_f(b[0] - 40)),          # F
    "10": PidDef("maf",          2, lambda b: _u16(b) / 100.0),             # g/s
    "11": PidDef("tps",          1, lambda b: b[0] * 100.0 / 255.0),        # %
    # O2 sensor voltage: byte0 / 200 V. byte1 is that sensor's own STFT
    # (0xFF = not used for trim) and is deliberately not decoded.
    "14": PidDef("o2_b1s1",      2, lambda b: b[0] / 200.0),                # V
    "15": PidDef("o2_b1s2",      2, lambda b: b[0] / 200.0),                # V
    "18": PidDef("o2_b2s1",      2, lambda b: b[0] / 200.0),                # V
    "19": PidDef("o2_b2s2",      2, lambda b: b[0] / 200.0),                # V
    "1F": PidDef("run_time",     2, lambda b: float(_u16(b))),              # s
    "2F": PidDef("fuel_level",   1, lambda b: b[0] * 100.0 / 255.0),        # %
    "33": PidDef("baro",         1, lambda b: float(b[0])),                 # kPa
    "3C": PidDef("cat_b1s1",     2, lambda b: _temp_f(_u16(b) / 10.0 - 40)),  # F
    "3D": PidDef("cat_b2s1",     2, lambda b: _temp_f(_u16(b) / 10.0 - 40)),  # F
    "42": PidDef("voltage",      2, lambda b: _u16(b) / 1000.0),            # V
    "43": PidDef("abs_load",     2, lambda b: _u16(b) * 100.0 / 255.0),     # %
    # 2 data bytes (SAE J1979; truck-mcp obd.py agrees). The 4-byte PIDs
    # are 24-2B and 34-3B; a wrong length here misaligns every PID after 44
    # in a batched reply.
    "44": PidDef("eq_ratio",     2, lambda b: _u16(b) * 2.0 / 65536.0),     # lambda
    "45": PidDef("rel_tps",      1, lambda b: b[0] * 100.0 / 255.0),        # %
    "46": PidDef("ambient",      1, lambda b: _temp_f(b[0] - 40)),          # F
    # Pedal sensors: on this truck PID 49 is APP D (the "app" channel); 47
    # and 4A are APP E and F.
    "47": PidDef("app_e",        1, lambda b: b[0] * 100.0 / 255.0),        # %
    "49": PidDef("app",          1, lambda b: b[0] * 100.0 / 255.0),        # %
    "4A": PidDef("app_f",        1, lambda b: b[0] * 100.0 / 255.0),        # %
    "4C": PidDef("cmd_throttle", 1, lambda b: b[0] * 100.0 / 255.0),        # %
    "52": PidDef("ethanol",      1, lambda b: b[0] * 100.0 / 255.0),        # %
}

# ---- GM enhanced parameters (mode 22 ReadDataByIdentifier) ----------------- #
# Confirmed on this truck: the E38 ECM answers mode 22 (22 1940 -> 62 1940 28).
# The T43 TCM did NOT answer 7E1 in testing because it is not there: on this
# truck the TCM is 7E2 -> 7EA (see vehnet.MODULES for the capture evidence).
# An entry goes in ONLY once its DID -> parameter + scaling has been correlated
# against something independent on this truck; entries are polled and merged
# into the sample exactly like PIDs, so a guessed one would reach the gauges.
#   key format:  (module_header_or_None, did_hex) : (canonical_key, decode(list[int]))
#   example:     (None, "1940"): ("some_engine_param", lambda b: b[0])
#
# Passing a single-point match is NOT verification: an entry must also be
# shown to TRACK the physical value through a transient (cold -> warm) on a
# drive. A DID that fails that check goes in REJECTED_DIDS, never back here.
DID_TABLE: dict = {}

# DIDs that were promoted and then failed live verification. Never polled;
# tests/test_gt_did_table.py fails while any of these is also in DID_TABLE,
# so re-promoting one means removing it here, with the new evidence, first.
#   key: (module_header_or_None, did_hex) -> (canonical_key, why rejected)
REJECTED_DIDS = {
    (None, "1644"): (
        "tft",
        "ECM DID 1644 byte1 was promoted as trans fluid temp (commit ee0189f, "
        "2026-08-18) from one match to the DIC (194 F). It read raw 0x83 "
        "(131 -> 91.0 C / 195.8 F) on all 1463 samples of the 2026-10-25 "
        "drive (gt_log_20261025_141431.csv, 2107 s) while coolant rose "
        "127 -> 203 F, and 0x0083 in every tools/snap_*.tsv snapshot "
        "(key-on, park, reverse, neutral, drive). A constant is not a live "
        "reading; most likely a default/substitute value."),
}

# Follow-up, NOT a channel: the candidate live trans temp source is the TCM
# (header 7E2 -> 7EA), DID 1940, byte0, GM (byte-40) C. truck-mcp saw
# 0x67-0x68 (63-64 C) there on 2026-08-03 and no answer on 2026-09-18, so it
# is unproven. To promote it: request_did("1940", module="7E2") across a
# cold -> warm drive, show it tracks (and differs from ECT), then add
#   ("7E2", "1940"): ("tft", lambda b: (b[0] - 40) * 9 / 5 + 32)
# here with that evidence cited. Until then the GT has no trans temp channel.
# canonical keys this transport can ever surface (for gauge pre-build)
CANONICAL_KEYS = sorted({d.key for d in PID_TABLE.values()}
                        | {v[0] for v in DID_TABLE.values()})
_PID_LENGTHS = {pid: d.nbytes for pid, d in PID_TABLE.items()}

_log = logging.getLogger(__name__)

OBDX_VID = 0x0483
OBDX_PID = 0x5740


def find_gt_ports(ports=None) -> list[str]:
    """COM ports whose USB VID/PID is the OBDX Pro GT's. `ports` defaults to
    pyserial's live enumeration; tests pass stand-ins."""
    if ports is None:
        ports = list(list_ports.comports()) if list_ports else []
    return [p.device for p in ports
            if p.vid == OBDX_VID and p.pid == OBDX_PID]


def describe_no_gt(ports=None) -> str:
    """Why autodetect gave up, naming every port seen so the user can pick."""
    if ports is None:
        ports = list(list_ports.comports()) if list_ports else []
    found = find_gt_ports(ports)
    seen = ", ".join(f"{p.device} ({p.description})" for p in ports) or "none"
    if len(found) > 1:
        return (f"More than one OBDX Pro GT found ({', '.join(found)}); "
                f"pass the port explicitly")
    return (f"No OBDX Pro GT (USB {OBDX_VID:04X}:{OBDX_PID:04X}) found. "
            f"Serial ports present: {seen}")


# --------------------------------------------------------------------------- #
# DTC / readiness parsing — pure functions, unit-tested without hardware
# --------------------------------------------------------------------------- #
def format_dtc(b1: int, b2: int) -> str:
    """Two DTC bytes -> SAE code string (P0300 style)."""
    letter = "PCBU"[(b1 >> 6) & 0x3]
    return f"{letter}{(b1 >> 4) & 0x3}{b1 & 0xF:X}{(b2 >> 4) & 0xF:X}{b2 & 0xF:X}"


def _hexonly(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch in "0123456789ABCDEF")


def reassemble_isotp(resp: str) -> dict[str, dict]:
    """Reassemble ISO-TP messages per responder from a headers-on reply.

    Adapter format this relies on (set in ObdxGt._prepare, confirmed with
    OK): ATH1 (CAN id shown), ATS0 (no spaces) and ATCAF1 (auto-format on;
    also the ATZ default in open()). In that mode every CAN frame is one line
    — '<3-hex id><PCI><data>' — and the reader flattens lines to
    whitespace-separated tokens, so one token is one frame. The ELM shows the
    PCI with headers on and trims a consecutive frame to the bytes that
    remain (seen on the truck 2026-09-26: '7E81008430303050606' then
    '7E8210700').

    Frames from different responders may interleave; each id gets its own
    reassembly. Returns {can_id: {"messages": [payload_hex, ...],
    "errors": [reason, ...]}}. A message whose length does not reach its
    declared length, a sequence gap, or a CF without an FF is an ERROR for
    that responder — never a silently short payload."""
    state: dict[str, dict] = {}

    def mod(cid):
        return state.setdefault(cid, {"messages": [], "errors": [],
                                      "_ff": None})

    for tok in resp.upper().split():
        if len(tok) < 5 or len(tok) % 2 == 0 or any(
                c not in "0123456789ABCDEF" for c in tok):
            continue                  # not '<3-hex id><whole bytes>'
        cid, data = tok[:3], tok[3:]
        m = mod(cid)
        kind, low = data[0], int(data[1], 16)
        if kind == "0":                                   # single frame
            if m["_ff"] is not None:
                m["errors"].append("single frame while a multi-frame "
                                   "message was incomplete")
                m["_ff"] = None
            body = data[2:]
            if low == 0 or len(body) < 2 * low:
                m["errors"].append(f"single frame shorter than its length "
                                   f"{low}")
                continue
            m["messages"].append(body[:2 * low])
        elif kind == "1":                                 # first frame
            if m["_ff"] is not None:
                m["errors"].append("new first frame before the previous "
                                   "message completed")
            if len(data) < 4:
                m["errors"].append("first frame without a length")
                m["_ff"] = None
                continue
            m["_ff"] = {"len": int(data[1:4], 16), "buf": data[4:], "sn": 1}
        elif kind == "2":                                 # consecutive frame
            ff = m["_ff"]
            if ff is None:
                m["errors"].append(f"consecutive frame {low} without a "
                                   "first frame")
                continue
            if low != ff["sn"]:
                m["errors"].append(f"sequence gap: expected frame "
                                   f"{ff['sn']}, got {low}")
                m["_ff"] = None
                continue
            ff["buf"] += data[2:]
            ff["sn"] = (ff["sn"] + 1) & 0xF
        else:
            continue                  # flow control etc. — not payload
        ff = m["_ff"]
        if ff is not None and len(ff["buf"]) >= 2 * ff["len"]:
            m["messages"].append(ff["buf"][:2 * ff["len"]])
            m["_ff"] = None
    for cid, m in state.items():
        ff = m.pop("_ff")
        if ff is not None:
            m["errors"].append(f"truncated: {len(ff['buf']) // 2} of "
                               f"{ff['len']} bytes")
    return state


def parse_dtc_reply(resp: str, mode: str) -> dict:
    """Mode 03/07/0A headers-on reply -> per-responder DTC lists.

    Walks each reassembled message by its declared structure: response mode
    byte (43/47/4A), count byte, then exactly count 2-byte DTCs. Nothing is
    ever re-scanned, so a 0x43 inside DTC data cannot start a phantom code.
    A 00 00 pair is not a DTC; bytes beyond the declared length (padding AA/
    55/00) never reach this walk.

    Returns {"codes": [...] de-duped across responders (order kept),
    "by_module": {id: [codes]} for responders with a complete answer,
    "incomplete": {id: reason} for responders whose answer could not be
    fully read (truncation, gap, count/length mismatch, negative
    response)}. A responder in "incomplete" is NOT EXAMINED — its codes are
    unknown, not absent."""
    rmode = "%02X" % (int(mode, 16) + 0x40)
    out = {"codes": [], "by_module": {}, "incomplete": {}}
    for cid, m in reassemble_isotp(resp).items():
        if m["errors"]:
            out["incomplete"][cid] = "; ".join(m["errors"])
            continue
        codes: list[str] = []
        problem = None
        answered = False              # a complete positive response was walked
        for msg in m["messages"]:
            if msg.startswith("7F"):
                problem = f"negative response 7F {msg[2:4]} {msg[4:6]}"
                break
            if not msg.startswith(rmode):
                continue              # a reply to something else
            if len(msg) < 4:
                problem = "response without a count byte"
                break
            n = int(msg[2:4], 16)
            pairs = msg[4:]
            if len(pairs) != 4 * n:
                problem = (f"count says {n} DTC(s) but the message carries "
                           f"{len(pairs) / 4:g}")
                break
            for j in range(0, len(pairs), 4):
                b1, b2 = int(pairs[j:j + 2], 16), int(pairs[j + 2:j + 4], 16)
                if b1 or b2:
                    codes.append(format_dtc(b1, b2))
            answered = True
        if problem:
            out["incomplete"][cid] = problem
        elif answered:
            out["by_module"][cid] = codes
    seen: set[str] = set()
    for cid in sorted(out["by_module"]):
        for c in out["by_module"][cid]:
            if c not in seen:
                seen.add(c)
                out["codes"].append(c)
    return out


# Continuous + non-continuous monitors for spark-ignition, OBD-II PID 01.
_CONT_MONITORS = ["Misfire", "Fuel system", "Components"]
_SPARK_MONITORS = ["Catalyst", "Heated catalyst", "EVAP system",
                   "Secondary air", "A/C refrigerant", "O2 sensor",
                   "O2 heater", "EGR system"]


# ISO 14229-1 DTC status bits that mean a test FAILED: testFailed (0),
# testFailedThisOperationCycle (1), pendingDTC (2), confirmedDTC (3),
# testFailedSinceLastClear (5), warningIndicatorRequested (7). Bits 4 and 6 are
# 'test not completed' and never make a record a fault on their own.
UDS_FAULT_STATUS_BITS = 0xAF


def parse_uds19_reply(resp: str, resp_id: Optional[str] = None) -> dict:
    """UDS ReadDTCInformation $19 02 (reportDTCByStatusMask) reply, headers-on.

    Shape: 59 02 <statusAvailabilityMask> then N records of 4 bytes each — a
    3-byte DTC (SAE 2-byte code + 1-byte failure-type byte) and a 1-byte status.
    Reassembled per responder (reuses reassemble_isotp); when resp_id is given
    only that id is parsed, so a physically addressed module's reply cannot be
    confused with another's. The GMLAN chassis/body modules (EBCM, BCM) answer
    this service; the powertrain modules use the 2-byte OBD modes instead.

    The request mask is FF, so a module also returns records whose only set
    bits are 'test not completed' (ISO 14229 status bits 4 and 6) — those are
    NOT faults. "codes" holds only records with a failure bit set
    (UDS_FAULT_STATUS_BITS); every record stays in "records".

    Returns {"codes": [SAE strings], "records": [(code, ftb, status)],
    "errors": [reason], "examined": bool}. An all-zero DTC (00 00 00) is the
    'no DTCs' filler and is skipped, never counted. 'examined' is True only when
    a 59 02 positive reply was actually seen — silence is never 'no codes'."""
    out: dict = {"codes": [], "records": [], "errors": [], "examined": False}
    for cid, m in reassemble_isotp(resp).items():
        if resp_id and cid.upper() != resp_id.upper():
            continue
        out["errors"].extend(m["errors"])
        for msg in m["messages"]:
            b = [int(msg[i:i + 2], 16) for i in range(0, len(msg) - 1, 2)]
            if len(b) < 3 or b[0] != 0x59 or b[1] != 0x02:
                continue
            out["examined"] = True
            rec = b[3:]                       # after 59 02 <availabilityMask>
            for i in range(0, len(rec) - 3, 4):
                d0, d1, ftb, status = rec[i], rec[i + 1], rec[i + 2], rec[i + 3]
                if d0 == 0 and d1 == 0 and ftb == 0:
                    continue                  # 'no DTC' filler record
                code = format_dtc(d0, d1)
                out["records"].append((code, ftb, status))
                if status & UDS_FAULT_STATUS_BITS and code not in out["codes"]:
                    out["codes"].append(code)
    return out


# GMLAN $A9 status byte (truck-mcp gmlan.py, verified on this truck
# 2026-08-03): bit1 = currently failed, bit7 = warning lamp; 01/19/21/25 are the
# housekeeping values healthy supported-DTC entries carry.
A9_BASELINE_STATUSES = frozenset({0x01, 0x19, 0x21, 0x25})


def a9_status_is_fault(status: str) -> bool:
    """True when an $A9 status byte marks a real fault, not a table entry.
    An unparseable status is treated as a fault — unknown is never clean."""
    try:
        v = int(status, 16)
    except (TypeError, ValueError):
        return True
    return bool(v & 0x02) or v not in A9_BASELINE_STATUSES


def parse_a9_report(resp: str, uudt_id: Optional[str] = None) -> dict:
    """GMLAN service $A9 81 (reportDTCByStatusMask) report — the read the GMLAN
    chassis/body modules (EBCM, BCM) answer instead of UDS $19, VERIFIED live on
    this truck via truck-mcp (gmlan.decode_a9_frame / A9_READ_BY_MASK).

    Report frames come back UUDT on request+0x300 (EBCM 0x243 -> 0x543), one per
    line: '<id>81<b1><b2><symptom><status>' where <b1><b2> is a 2-byte GMLAN DTC
    (0x4035 -> C0035, via format_dtc) and 00 00 is the end-of-table marker. A
    negative response arrives USDT on request+0x400: '<id>037FA9<nrc>'.

    When uudt_id is given only that id's frames are counted, so a neighbouring
    module's report cannot bleed in.

    THE TABLE IS NOT THE FAULT LIST. With mask FF the module reports EVERY DTC
    its calibration supports, each with a status byte; a healthy entry carries a
    housekeeping status (01/19/21/25, observed on this truck by truck-mcp). A
    code is a FAULT only when its status has bit1 (currently failed) set or is
    not a housekeeping value — the same rule as truck-mcp gmlan.fault_codes
    (a live C0035 reads status D3; the 2026-10-04 post-SPS capture read 01).
    Unknown statuses count as faults: an unrecognised status is never clean.

    Returns {"codes": [SAE faults], "table": [every SAE code reported],
    "records": [(code, symptom, status)] (unique triples), "examined": bool,
    "negative": nrc|None, "complete": bool}. 'examined' is True once any $A9
    report frame (even the 00 00 marker) or a negative is seen — silence stays
    'not examined', never 'no codes'. 'complete' is True only when the 00 00
    END-OF-TABLE marker arrived: a capture cut short (the adapter stopped
    listening mid-table — seen 2026-10-04, the BCM read 46 of 66 entries over
    ELM) is examined but NOT complete, and must be reported as such."""
    out: dict = {"codes": [], "table": [], "records": [], "examined": False,
                 "negative": None, "complete": False}
    want = uudt_id.upper() if uudt_id else None
    seen: set = set()
    for tok in resp.upper().split():
        c = "".join(ch for ch in tok if ch in "0123456789ABCDEF")
        # negative response, with or without a 3-char CAN id prefix.
        if len(c) >= 6 and c[0:2] == "7F" and c[2:4] == "A9":
            out["negative"] = c[4:6]
            continue
        if len(c) >= 11 and c[5:7] == "7F" and c[7:9] == "A9":
            out["negative"] = c[9:11]
            continue
        # $A9 report frame '81 <b1> <b2> <symptom> <status>'. VERIFIED on the GT
        # 2026-10-04: with ATCRA + headers off the GT returns it WITHOUT a CAN id
        # prefix (e.g. '8140355A01000000' = C0035). A 3-char-id-prefixed form is
        # also accepted for completeness.
        if c[0:2] == "81" and len(c) >= 10:
            off = 0
        elif len(c) >= 13 and c[3:5] == "81":
            if want and c[0:3] != want:
                continue
            off = 3
        else:
            continue
        try:
            b1 = int(c[off + 2:off + 4], 16)
            b2 = int(c[off + 4:off + 6], 16)
            symptom = c[off + 6:off + 8]
            status = c[off + 8:off + 10]
        except ValueError:
            continue
        out["examined"] = True
        if b1 == 0 and b2 == 0:
            out["complete"] = True                               # end-of-table
            continue
        code = format_dtc(b1, b2)
        rec = (code, symptom, status)
        if rec in seen:           # the module repeats frames within one capture
            continue
        seen.add(rec)
        out["records"].append(rec)
        if code not in out["table"]:
            out["table"].append(code)
        # a DTC repeats with different symptom bytes; it is a fault if ANY of
        # its entries is, so evaluate every record, not just the first one.
        if a9_status_is_fault(status) and code not in out["codes"]:
            out["codes"].append(code)
    return out


def parse_readiness(data: list[int]) -> dict:
    """PID 0101 payload (4 bytes) -> MIL, DTC count, monitor table."""
    if not data or len(data) < 4:
        return {}
    a, b, c, d = data[:4]
    monitors = []
    for bit, name in enumerate(_CONT_MONITORS):
        if b & (1 << bit):
            monitors.append((name, not bool(b & (1 << (bit + 4)))))
    for bit, name in enumerate(_SPARK_MONITORS):
        if c & (1 << bit):
            monitors.append((name, not bool(d & (1 << bit))))
    return {
        "mil": bool(a & 0x80),
        "dtc_count": a & 0x7F,
        "monitors": monitors,   # (name, complete)
    }


def parse_hs_responders(resp: str) -> set[str]:
    """Headers-on functional 0100 reply -> set of responding CAN ids
    (7E8..7EF). Every responder line is collected: the reader flattens the
    ELM's CR-separated lines into one string, so each '7Ex <PCI> 41 00' is
    matched wherever it sits. The PCI byte is pinned to a single-frame length
    (01..07) so data bytes cannot masquerade as a header."""
    return set(re.findall(r"(7E[89A-F])0[1-7]4100", _hexonly(resp)))


def is_binary_reply(raw: bytes) -> bool:
    """True when a reply to a TEXT command is not text. After a J2534 session
    the OBDX GT stays in its binary J2534 mode and answers AT commands with
    binary frames (observed on the truck 2026-09-26: ATI answered with the
    five bytes 7F 02 41 01 3C). ELM text is printable ASCII plus CR/LF/TAB
    and the '>' prompt; anything else (DEL 0x7F, control bytes, >= 0x80)
    means the GT is not speaking ELM."""
    return any(not (0x20 <= b < 0x7F or b in (0x0D, 0x0A, 0x09)) for b in raw)


#: The one recovery that is known to work (2026-09-26, 2026-10-01): the GT is
#: USB-powered, so cycling the DLC or the vehicle alone does not reset it.
GT_BINARY_REMEDY = ("Unplug the GT's USB cable (and the OBD plug) for 10 s, "
                    "plug it back in, then connect again.")


class GtBinaryMode(RuntimeError):
    """The GT is answering text (ELM) commands in its binary J2534 mode."""


class GtNoResponse(TimeoutError):
    """The GT sent nothing back (no '>' prompt) for a poll request. An ELM
    always ends a reply with the prompt, even 'NO DATA', so silence means the
    adapter or its link is gone, not that the vehicle is quiet."""


def is_elm_identity(ati: str, at1: str) -> bool:
    """Positive identification of an ELM/OBDX text interface: ATI names an
    ELM327 or AT@1 names the OBDX. Any other bytes are not proof of life."""
    return "ELM" in ati.upper() or "OBDX" in at1.upper()


def at_reply_ok(resp: str) -> bool:
    """An AT configuration command was accepted: the reply carries OK and no
    '?' (the ELM's 'unknown/rejected command' answer)."""
    return "OK" in resp.upper().split() and "?" not in resp


def reply_has_frame_from(resp: str, can_id: str) -> bool:
    """Headers-on reply contains a frame whose CAN id is can_id. Matches frame
    STARTS only: with spaces off (ATS0) each frame is one token beginning with
    its id; with spaces on the id is its own 3-char token while data bytes are
    2-char tokens. A plain substring test would let data bytes fake an id
    (e.g. '7E8 06 43 ...' contains '643')."""
    want = can_id.upper()
    for tok in resp.upper().split():
        if (len(tok) >= 3 and tok.startswith(want)
                and all(c in "0123456789ABCDEF" for c in tok)):
            return True
    return False


def parse_frames(resp: str) -> list[tuple[str, str]]:
    """Headers-on, spaces-off (ATH1 ATS0) single-frame reply -> [(can_id,
    payload_hex)]. Each flattened token is one frame: 3-hex id, PCI length
    byte, then that many payload bytes. Tokens that are not well-formed
    single frames (NO DATA, '?', multi-frame) are skipped, never guessed."""
    frames = []
    for tok in resp.upper().split():
        if len(tok) < 5 or any(c not in "0123456789ABCDEF" for c in tok):
            continue
        n = int(tok[3:5], 16)
        if not 1 <= n <= 7 or len(tok) < 5 + 2 * n:
            continue
        frames.append((tok[:3], tok[5:5 + 2 * n]))
    return frames


_HEX = frozenset("0123456789ABCDEF")
# ELM multi-frame line with headers off: '<frame index>:<data>' (index is one
# hex digit, 0..F, wrapping). The index is NOT data.
_MF_LINE = re.compile(r"^([0-9A-F]):([0-9A-F]*)$")


def _hex_bytes(h: str) -> list[int]:
    h = h[:len(h) - (len(h) % 2)]
    return [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]


def mode01_message(resp: str) -> Optional[list[int]]:
    """The response MESSAGE bytes of a reply to one Mode 01 request (see
    service_message, which reads every service the same way)."""
    return service_message(resp, 0x01)


def service_message(resp: str, service: int) -> Optional[list[int]]:
    """The response MESSAGE bytes (service byte first) of a reply to one
    request for OBD `service` (0x01 data, 0x09 vehicle information), or None
    when there is none (NO DATA, '?', garbage). A single-frame or headers-on
    message is only taken when it starts with the positive response byte
    (service + 0x40) or a negative response (7F).

    ELM327 text formats this reads (ATS0 ATCAF1, from ObdxGt.open):
      single frame, headers off:  '410C1AF8'
      multi-frame, headers off:   '01A' '0:410C1AF80D20' '1:...' '2:...'
          The first line is the 3-hex-digit total message length; every
          frame line carries a one-digit index and a colon. Neither is data:
          stripping the reply to hex digits (the old _extract) turned them
          into payload bytes and misaligned every value after them. Frames
          are joined in index order (a gap ends the message there) and cut
          at the declared length, which drops the last frame's padding.
      headers on (ATH1), when the ATH0 restore after a diagnostic did not
          take: reassembled per responder by reassemble_isotp; the ECM's
          (7E8) message is used.
    Spaces on (a 'N:' line followed by 2-digit byte tokens) is accepted too.
    Ported from truck-mcp obd.py frames_by_module / parse_multi_pid."""
    declared: Optional[int] = None
    frames: list[list] = []          # [index, hex data]
    loose: list[str] = []
    for tok in resp.upper().split():
        m = _MF_LINE.match(tok)
        if m:
            frames.append([int(m.group(1), 16), m.group(2)])
            continue
        if any(c not in _HEX for c in tok):
            continue                 # NO DATA, SEARCHING..., STOPPED, '?'
        if _is_pending(tok, service):
            continue                 # 7F <svc> 78: the final reply follows
        if frames:
            frames[-1][1] += tok     # spaces on: bytes after 'N:'
        elif declared is None and not loose and len(tok) == 3:
            declared = int(tok, 16)  # multi-frame length line
        else:
            loose.append(tok)
    if frames:
        buf = ""
        for k, (idx, data) in enumerate(frames):
            if idx != k % 16:
                break                # missing frame: nothing after it aligns
            buf += data
        msg = _hex_bytes(buf)
        return msg[:declared] if declared is not None else msg
    if loose and all(len(t) == 2 for t in loose):
        loose = ["".join(loose)]     # spaces on, single frame
    prefixes = ("%02X" % (service + 0x40), "7F")
    for tok in loose:
        if tok.startswith(prefixes):
            return _hex_bytes(tok)
    ecm = reassemble_isotp(resp).get("7E8")
    if ecm:
        for msg in ecm["messages"]:
            if msg.startswith(prefixes) and not _is_pending(msg, service):
                return _hex_bytes(msg)
    return None


#: Negative response code 0x78: requestCorrectlyReceived-ResponsePending.
#: Not an answer: the module sends its real reply after it.
NRC_RESPONSE_PENDING = 0x78


def _is_pending(hex_msg: str, service: int) -> bool:
    return hex_msg.upper().startswith("7F%02X%02X" % (service,
                                                       NRC_RESPONSE_PENDING))


def response_pending_only(resp: str, service: int) -> bool:
    """True when the reply holds a 7F <service> 78 (response pending) and
    nothing else usable: the module was still working when the read ended."""
    if service_message(resp, service) is not None:
        return False
    toks = [t for t in resp.upper().split() if all(c in _HEX for c in t)]
    msgs = toks + [m for mod in reassemble_isotp(resp).values()
                   for m in mod["messages"]]
    return any(_is_pending(t, service) for t in msgs)


def parse_mode01_reply(resp: str, pids: list[str],
                       lengths: dict[str, int]) -> dict[str, list[int]]:
    """Decode the reply to ONE Mode 01 request for `pids` (1..6 of them)
    into {pid: data bytes} for every requested PID present.

    The ECM answers '41' then, for each PID it chose to answer, the PID byte
    and its data, in ANY order, omitting PIDs it does not support. The walk
    only advances over PIDs that were requested and not yet seen, with the
    byte length from `lengths`: a trailing 0x00 pad is a legal PID id, and
    accepting it would read padding as data (truck-mcp parse_multi_pid). A
    PID cut short by a truncated reply is dropped, never padded out. A
    negative response (7F 01 nrc) or no message yields {}."""
    msg = mode01_message(resp)
    if not msg or msg[0] != 0x41:
        return {}
    remaining = {p.upper() for p in pids}
    out: dict[str, list[int]] = {}
    i = 1
    while i < len(msg) and remaining:
        pid = "%02X" % msg[i]
        if pid not in remaining:
            break
        n = lengths[pid]
        data = msg[i + 1:i + 1 + n]
        if len(data) < n:
            break
        out[pid] = data
        remaining.discard(pid)
        i += 1 + n
    return out


#: Mode 09 info types read at recording start, with the size of ONE item.
#: J1979 / ISO 15765-4: on CAN every 49 reply carries a count byte (NODI)
#: after the info type, then count items: VIN 1 x 17 ASCII bytes, CALID n x
#: 16 ASCII bytes (NUL padded), CVN n x 4 bytes. Shape checked against the
#: real 7E8 replies truck-mcp captured on this truck (captures/
#: first-connect-2026-08-03.json: 49 02 01 + 17 VIN bytes; 49 04 08 + 8 x 16).
MODE09_ITEMS = {"02": ("vin", 17), "04": ("calids", 16), "06": ("cvns", 4)}


def parse_mode09_reply(resp: str, info_type: str) -> dict:
    """Decode the reply to ONE physical Mode 09 request ('09' + info_type).

    Returns {"examined": True, "raw": message hex, "count": n, "items":
    [[bytes], ...]} when the ECM answered 49 <type> <count> with exactly
    count whole items, else {"examined": False, "why": reason} (plus "raw"
    when a message was there but did not decode). A short or over-long body
    is an error, never padded or truncated into a value."""
    info_type = info_type.upper()
    _name, size = MODE09_ITEMS[info_type]
    msg = service_message(resp, 0x09)
    if msg is None and response_pending_only(resp, 0x09):
        return {"examined": False,
                "why": "not examined (response pending, no final reply)"}
    if msg is None:
        text = " ".join(resp.split()) or "(empty)"
        return {"examined": False,
                "why": f"no Mode 09 message in the reply: {text[:60]!r}"}
    raw = "".join("%02X" % b for b in msg)
    if msg[0] == 0x7F:
        nrc = msg[2] if len(msg) > 2 else None
        return {"examined": False, "raw": raw,
                "why": "ECM refused 09" + info_type + (
                    f" (negative response NRC 0x{nrc:02X})" if nrc is not None
                    else " (negative response)")}
    if len(msg) < 3 or msg[0] != 0x49 or "%02X" % msg[1] != info_type:
        return {"examined": False, "raw": raw,
                "why": f"reply is not 49 {info_type} <count> ..."}
    count, body = msg[2], msg[3:]
    if count == 0 or len(body) != count * size:
        return {"examined": False, "raw": raw,
                "why": f"count byte {count} x {size} bytes does not match "
                       f"the {len(body)} data bytes received"}
    items = [body[i:i + size] for i in range(0, len(body), size)]
    return {"examined": True, "raw": raw, "count": count, "items": items}


def _ascii_item(item: list[int]) -> Optional[str]:
    """Printable ASCII with NUL padding stripped, or None when any byte is
    not printable (a non-text item is reported as raw hex only)."""
    text = bytes(item).rstrip(b"\x00")
    if not text or any(b < 0x20 or b > 0x7E for b in text):
        return None
    return text.decode("ascii")


def decode_vehicle_identity(parsed: dict) -> dict:
    """{"vin"|"calids"|"cvns": parse_mode09_reply result} -> the identity
    entries ObdxGt.read_vehicle_identity returns: every item's raw hex plus
    decoded text where the item is text (VIN, CALID). A VIN that is not 17
    VIN characters keeps its raw hex and says why it is not a VIN."""
    out: dict = {}
    for _info_type, (name, _size) in MODE09_ITEMS.items():
        p = parsed.get(name) or {"examined": False, "why": "not requested"}
        if not p.get("examined"):
            out[name] = {k: v for k, v in p.items() if k != "items"}
            continue
        items_hex = ["".join("%02X" % b for b in it) for it in p["items"]]
        if name == "vin":
            text = _ascii_item(p["items"][0])
            if text is None or len(text) != 17 or not text.isalnum():
                out[name] = {"examined": False, "raw": p["raw"],
                             "item_hex": items_hex[0],
                             "why": "VIN bytes are not 17 printable VIN "
                                    "characters"}
            else:
                out[name] = {"examined": True, "raw": p["raw"],
                             "item_hex": items_hex[0], "value": text}
        elif name == "calids":
            out[name] = {"examined": True, "raw": p["raw"],
                         "count": p["count"],
                         "items": [{"hex": h, "text": _ascii_item(it)}
                                   for h, it in zip(items_hex, p["items"])]}
        else:
            out[name] = {"examined": True, "raw": p["raw"],
                         "count": p["count"],
                         "items": [{"hex": h} for h in items_hex]}
    return out


def parse_atrv(resp: str) -> Optional[float]:
    """ATRV text reply ('12.6V') -> volts, or None when it does not parse.
    None means UNREADABLE, never 'no voltage'."""
    m = re.fullmatch(r"\s*(\d{1,2}(?:\.\d{1,2})?)\s*V\s*", resp.upper())
    return float(m.group(1)) if m else None


# Common-code descriptions (generic OBD-II; blank when unknown — never guess).
DTC_DESCRIPTIONS = {
    "P0011": "Intake camshaft position timing over-advanced",
    "P0030": "HO2S heater control circuit (bank 1 sensor 1)",
    "P0053": "HO2S heater resistance (bank 1 sensor 1)",
    "P0101": "MAF sensor performance",
    "P0102": "MAF sensor circuit low",
    "P0106": "MAP sensor performance",
    "P0113": "IAT sensor circuit high",
    "P0117": "ECT sensor circuit low",
    "P0118": "ECT sensor circuit high",
    "P0121": "TPS performance",
    "P0128": "Coolant temp below thermostat regulating temperature",
    "P0131": "HO2S circuit low voltage (bank 1 sensor 1)",
    "P0135": "HO2S heater performance (bank 1 sensor 1)",
    "P0171": "Fuel trim system lean (bank 1)",
    "P0172": "Fuel trim system rich (bank 1)",
    "P0174": "Fuel trim system lean (bank 2)",
    "P0175": "Fuel trim system rich (bank 2)",
    "P0200": "Injector control circuit",
    "P0300": "Engine misfire detected (random/multiple)",
    "P0301": "Cylinder 1 misfire detected",
    "P0302": "Cylinder 2 misfire detected",
    "P0303": "Cylinder 3 misfire detected",
    "P0304": "Cylinder 4 misfire detected",
    "P0305": "Cylinder 5 misfire detected",
    "P0306": "Cylinder 6 misfire detected",
    "P0307": "Cylinder 7 misfire detected",
    "P0308": "Cylinder 8 misfire detected",
    "P0325": "Knock sensor circuit (bank 1)",
    "P0332": "Knock sensor circuit low (bank 2)",
    "P0420": "Catalyst efficiency below threshold (bank 1)",
    "P0430": "Catalyst efficiency below threshold (bank 2)",
    "P0442": "EVAP system small leak detected",
    "P0446": "EVAP vent solenoid performance",
    "P0455": "EVAP system large leak detected",
    "P0463": "Fuel level sensor circuit high",
    "P0521": "Engine oil pressure sensor performance",
    "P0700": "Transmission control system malfunction (TCM has codes)",
    "P0711": "Trans fluid temp sensor performance",
    "P0742": "TCC system stuck on",
    "P0894": "Transmission component slipping",
    "U0100": "Lost communication with ECM",
    "U0101": "Lost communication with TCM",
    "U0121": "Lost communication with EBCM (ABS)",
    "U0140": "Lost communication with BCM",
}


class ObdxGt:
    #: Consecutive cycles in which a batch is refused (singles answer) before
    #: batching is given up for the connection. One is not enough: a lost
    #: frame or CAN ERROR looks the same and would cost ~6x throughput.
    REFUSAL_CYCLES = 2

    def __init__(self, port: Optional[str] = None, baud: int = 115200,
                 timeout: float = 0.5):
        self.port_name = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None
        self.supported: set[str] = set()
        # Raw 32-bit supported-PID masks from the last probe, by request
        # ('0100'..'0160'); _probe_stop says where and why the probe ended.
        self.supported_masks: dict[str, int] = {}
        self._probe_stop: Optional[tuple[str, str]] = None
        self.device = "?"
        # The tx header the GT is KNOWN to be using, or None when a header
        # command was refused and the real header is unknown. Requests whose
        # meaning depends on addressing must not go out while this is None.
        self._header: Optional[str] = None
        # Per-reply read deadline; a binary-mode GT never sends '>' so every
        # read in that state runs to this deadline.
        self.read_deadline_s = 1.2
        self.last_raw = b""
        # time.monotonic() just before the last command() was written and
        # just after its reply was read: the exchange that dates its value.
        self.last_exchange: Optional[tuple[float, float]] = None
        # Sequence number of the data requests issued by poll_timed(): one
        # per request on the link, shared by every value one reply decoded.
        self._request_seq = 0
        # Batched, tiered polling (pollplan). The plan is rebuilt whenever
        # the supported set or the batch size changes (connect / reconnect).
        self._plan: Optional[pollplan.PollPlan] = None
        self._plan_for: Optional[tuple] = None
        self._cycle = 0
        # Set when the ECM answered PIDs singly that it would not answer as
        # a batch: from then on (until the next open) every PID goes alone.
        self.batching_refused = False
        self.batch_refusals = 0          # cycles with a batch refused
        self._refusal_streak = 0         # consecutive such cycles

    @staticmethod
    def autodetect() -> Optional[str]:
        """The GT's COM port, matched by USB VID/PID only. None when there is
        no GT or more than one: guessing another port would hand the ELM
        command stream to whatever else is attached (the OBDLink MX+ is also
        ELM-compatible), so open() fails loudly instead."""
        found = find_gt_ports()
        return found[0] if len(found) == 1 else None

    # -- lifecycle --------------------------------------------------------- #
    def open(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial not installed (uv pip install pyserial)")
        if not self.port_name:
            self.port_name = self.autodetect()
        if not self.port_name:
            raise RuntimeError(describe_no_gt())
        try:
            # write_timeout: a Bluetooth SPP port whose far end is gone can
            # block WriteFile indefinitely; that would wedge the poll thread
            # (and anything waiting on its IO lock) instead of failing.
            self.ser = serial.Serial(self.port_name, self.baud,
                                     timeout=self.timeout, write_timeout=2.0)
        except Exception as exc:
            text = str(exc)
            if isinstance(exc, PermissionError) or "Access is denied" in text \
                    or "PermissionError" in text:
                raise RuntimeError(
                    f"{self.port_name} is in use by another program (truck-mcp, "
                    f"the OBDX app, a J2534 tool or a second OpenOBD); close "
                    f"it and connect again ({text})") from exc
            raise
        try:
            time.sleep(0.2)
            # IDENTIFY BEFORE CONFIGURING. After a J2534 session (an e38flash
            # read) the GT stays in its binary mode and answers every text
            # command with binary frames; the old open() configured it blind,
            # took the binary AT@1 reply as the device name, and the Dashboard
            # then sat empty with no explanation (the Module Map path already
            # caught this; the Dashboard path did not — 2026-10-01).
            ati = self.command("ATI", wait=0.2)
            self._refuse_binary("ATI")
            self.command("ATZ", wait=0.9)
            self._refuse_binary("ATZ")
            for c in ("ATE0", "ATL0", "ATS0", "ATH0", "ATSP0"):
                self.command(c, wait=0.2)
            at1 = self.command("AT@1", wait=0.2)
            self._refuse_binary("AT@1")
            if not is_elm_identity(ati, at1):
                raise RuntimeError(
                    f"{self.port_name} did not identify as an ELM327/OBDX "
                    f"interface (ATI={ati!r}, AT@1={at1!r})")
            self.device = at1.strip() or "OBDX Pro GT"
            self._probe_supported()
            # A new link may be a different truck/ECM: batching is tried
            # again and the plan is rebuilt from the new supported set.
            self.batching_refused = False
            self._refusal_streak = 0
            self._plan = None
            self._set_header("7E0")  # physical ECM addr (Mode 01 batches, DIDs)
        except Exception:
            self.close()         # never leave the port held after a failed open
            raise

    def _refuse_binary(self, cmd: str) -> None:
        if is_binary_reply(self.last_raw):
            raise GtBinaryMode(
                f"the OBDX GT answered {cmd} in its binary (J2534) mode "
                f"({self.last_raw[:16]!r}) — a J2534 tool used it last. "
                f"{GT_BINARY_REMEDY}")

    def close(self) -> None:
        if self.ser:
            try:
                self.ser.close()
            finally:
                self.ser = None

    # -- raw io ------------------------------------------------------------ #
    def command(self, cmd: str, wait: float = 0.0,
                deadline: Optional[float] = None) -> str:
        """Send one command; returns the reply as flattened text. The raw
        bytes are kept in self.last_raw so callers can tell a binary-mode
        reply from ELM text (the lossy decode alone would hide it).

        deadline overrides the default read window — a raw GMLAN $A9 report
        trickles several UUDT frames in over ~1 s, longer than a normal command,
        so the capture must wait for them (no prompt arrives until they stop)."""
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        sent = time.monotonic()
        self.ser.write((cmd + "\r").encode())
        if wait:
            time.sleep(wait)
        reply = self._read_to_prompt(deadline_s=deadline)
        self.last_exchange = (sent, time.monotonic())
        return reply

    def request_raw(self, mode_pid: str) -> str:
        return self.command(mode_pid, wait=0.0)

    def _read_to_prompt(self, deadline_s: Optional[float] = None) -> str:
        buf = b""
        end = time.monotonic() + (self.read_deadline_s if deadline_s is None
                                  else deadline_s)
        while time.monotonic() < end:
            n = getattr(self.ser, "in_waiting", 0)
            if n:
                buf += self.ser.read(n)
                if b">" in buf:
                    break
            else:
                time.sleep(0.005)
        self.last_raw = buf
        return (buf.decode(errors="ignore")
                .replace("\r", " ").replace("\n", " ").replace(">", " ").strip())

    # -- checked configuration ---------------------------------------------- #
    def _at_checked(self, cmd: str, wait: float = 0.05) -> bool:
        """Send an AT config command and confirm the GT accepted it. The real
        GT intermittently answers '?' (seen: ATH1 -> OK, then ATSH7E0 -> ?),
        after which the next request silently goes out with the PREVIOUS
        setting — so every setting a result depends on is checked, and
        retried once."""
        for _ in range(2):
            resp = self.command(cmd, wait=wait)
            if not is_binary_reply(self.last_raw) and at_reply_ok(resp):
                return True
        return False

    def _set_header(self, header: str) -> bool:
        """Set the tx header and track the header actually in force."""
        if self._at_checked("ATSH" + header):
            self._header = header.upper()
            return True
        self._header = None
        return False

    # -- pid decode -------------------------------------------------------- #
    @staticmethod
    def _extract(resp: str, mode_pid: str) -> Optional[list]:
        s = "".join(ch for ch in resp.upper() if ch in "0123456789ABCDEF")
        rmode = "%02X" % (int(mode_pid[:2], 16) + 0x40)
        header = rmode + mode_pid[2:].upper()
        idx = s.find(header)
        if idx < 0:
            return None
        rest = s[idx + len(header):]
        rest = rest[:len(rest) - (len(rest) % 2)]
        return [int(rest[i:i + 2], 16) for i in range(0, len(rest), 2)]

    def _probe_supported(self) -> None:
        self.supported = set()
        self.supported_masks = {}
        self._probe_stop = None
        for base in ("0100", "0120", "0140", "0160"):
            resp = self.request_raw(base)
            data = self._extract(resp, base)
            if not data or len(data) < 4:
                self._probe_stop = (base, "no supported-PID answer")
                break
            start = int(base[2:], 16)
            mask = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
            self.supported_masks[base] = mask
            for bit in range(32):
                if mask & (1 << (31 - bit)):
                    self.supported.add("%02X" % (start + 1 + bit))
            if not (mask & 1):  # bit for "next range supported"
                self._probe_stop = (base, "its next-range bit is clear")
                break

    def supported_report(self) -> dict:
        """The four Mode 01 supported-PID masks as 8-digit hex; a range with
        no mask is {"examined": False, "why": ...}. Plus the decoded PIDs."""
        masks: dict = {}
        stop = self._probe_stop
        for base in ("0100", "0120", "0140", "0160"):
            if base in self.supported_masks:
                masks[base] = "%08X" % self.supported_masks[base]
            elif stop and stop[0] == base:
                masks[base] = {"examined": False, "why": stop[1]}
            elif stop:
                masks[base] = {"examined": False,
                               "why": f"not asked: {stop[0]} {stop[1]}"}
            else:
                masks[base] = {"examined": False,
                               "why": "supported PIDs not probed on this link"}
        return {"masks": masks, "pids": sorted(self.supported)}

    def possible_keys(self) -> list[str]:
        """Every canonical key poll_timed() can produce on this connection:
        the PIDs the ECM said it supports (all of PID_TABLE before the probe),
        the ATRV voltage fallback, and the mode-22 DIDs. The recorder fixes its
        columns from this, so a PID that only starts answering mid-drive (key
        on after connect, engine start) still lands in the log."""
        keys = {d.key for pid, d in PID_TABLE.items()
                if not self.supported or pid in self.supported}
        keys.add("voltage")
        keys |= {key for key, _fn in DID_TABLE.values()}
        return sorted(keys)

    # -- polling plan ------------------------------------------------------- #
    def poll_plan(self) -> pollplan.PollPlan:
        """The plan for the current supported set and batching state."""
        size = 1 if self.batching_refused else pollplan.MAX_PIDS_PER_REQUEST
        want = (frozenset(self.supported), size, len(DID_TABLE))
        if self._plan is None or self._plan_for != want:
            self._plan = pollplan.PollPlan.build(
                {pid: d.key for pid, d in PID_TABLE.items()}, self.supported,
                batch_size=size, n_dids=len(DID_TABLE))
            self._plan_for = want
            self._cycle = 0          # a new plan starts with a full cycle
            _log.info("GT %s %s", self.port_name, self._plan.describe())
        return self._plan

    def poll_once(self) -> dict:
        """One FULL sweep (every tier) as {key: value} (CLI, diagnostics).
        Recording uses poll_timed(), which keeps each value's own request
        time and follows the tier schedule."""
        return {r.key: r.value for r in self.poll_timed(full=True)}

    def _next_request(self) -> tuple[int, tuple[float, float]]:
        """Number the request just made and return (number, its exchange)."""
        self._request_seq += 1
        return self._request_seq, self.last_exchange

    @staticmethod
    def _reading(out: list, key: str, fn, data, request: int,
                 exchange: tuple[float, float]) -> None:
        """Decode `data` into a Reading carrying its request's number and
        send/reply times. Several readings may come from one request (a
        batched reply); they share both. A decode failure drops the value
        and the request number stays consumed, so the gap is visible."""
        try:
            value = float(fn(data))
        except Exception:
            return
        sent, received = exchange
        out.append(Reading(key, value, sent, received, request))

    def _request_pids(self, out: list, pids: list[str]) -> int:
        """One Mode 01 request for `pids`; appends a Reading per PID the ECM
        answered and returns how many decoded. Raises GtNoResponse when the
        GT sent no prompt at all."""
        cmd = "01" + "".join(pids)
        resp = self.request_raw(cmd)
        request, exchange = self._next_request()
        if b">" not in self.last_raw:
            # Fail the poll now: walking the rest of the plan at the read
            # deadline each would take ~30 s to report a dead GT.
            raise GtNoResponse(
                f"no reply from the GT on {self.port_name} for {cmd}")
        got = parse_mode01_reply(resp, pids, _PID_LENGTHS)
        before = len(out)
        for pid in pids:                      # request order, not reply order
            if pid in got:
                d = PID_TABLE[pid]
                self._reading(out, d.key, d.decode, got[pid], request,
                              exchange)
        return len(out) - before

    def poll_timed(self, full: bool = False) -> list:
        """One poll CYCLE as a list of Readings, in request order.

        The cycle asks for the plan's due PIDs (pollplan: FAST every cycle,
        MEDIUM every 4th, SLOW every 20th; cycle 0 and full=True ask for
        all), up to six per request. Each Reading carries the exchange of the
        request that returned it, so a value is bounded by its own request
        (2026-10-25, brain 8d92fae5); values from one batched reply share
        it. Only readings actually obtained are returned: a not-due or
        unanswered PID has NO reading, never a carried one.

        A batch the ECM answers with nothing (NO DATA / negative response)
        is retried one PID per request in the same cycle. If the ECM answers
        those, the batch was refused; if it answers none, the ECM is quiet
        and the rest of the cycle is not re-asked. One refused cycle can be a
        lost frame or a CAN ERROR, so only REFUSAL_CYCLES consecutive
        refusing cycles set batching_refused (one PID per request until the
        next open); a cycle whose batches answer resets the count."""
        out: list = []
        if not self.ser:
            return out
        # Physical ECM addressing is what makes these values ECM values (and
        # is what lets a multi-frame batch be flow-controlled: one
        # responder). If a header command was refused earlier, re-establish
        # it; if the GT still refuses, report nothing rather than another
        # module's bytes.
        if self._header != "7E0" and not self._set_header("7E0"):
            return out
        plan = self.poll_plan()
        cycle = 0 if full else self._cycle
        ecm_quiet = False
        refused: Optional[list] = None   # a batch refused in this cycle
        batched_ok = False               # a multi-PID batch answered
        for batch in plan.batches_for_cycle(cycle):
            if self.batching_refused and len(batch) > 1:
                for pid in batch:
                    self._request_pids(out, [pid])
                continue
            if self._request_pids(out, batch):
                batched_ok = batched_ok or len(batch) > 1
                continue
            if len(batch) == 1 or ecm_quiet:
                continue
            answered = sum(self._request_pids(out, [pid]) for pid in batch)
            if answered:
                refused = refused or batch
            else:
                ecm_quiet = True
        if refused is not None:
            self.batch_refusals += 1
            self._refusal_streak += 1
            if self._refusal_streak >= self.REFUSAL_CYCLES:
                self.batching_refused = True
                _log.warning("GT %s: the ECM answered %s singly but not as "
                             "one batched request on %d consecutive cycles "
                             "(%d refusing cycles this connection); polling "
                             "one PID per request until the next connect",
                             self.port_name, "01" + "".join(refused),
                             self._refusal_streak, self.batch_refusals)
            else:
                _log.info("GT %s: batch %s refused once (%d refusing cycles "
                          "this connection); still batching",
                          self.port_name, "01" + "".join(refused),
                          self.batch_refusals)
        elif batched_ok:
            self._refusal_streak = 0
        # Module voltage from the GT's own ATRV when PID 42 gave none, on the
        # SLOW tier where voltage lives. Also on any cycle where nothing at
        # all decoded: a healthy GT always answers ATRV, so the transport can
        # tell a quiet ECM (key off) from a dead link.
        if ((plan.tier_due(pollplan.SLOW, cycle) or not out)
                and not any(r.key == "voltage" for r in out)):
            # Strict parse, as the Module Map does: a binary or garbled reply
            # is UNREADABLE and leaves the gauge empty, never a made-up value
            # pulled out of noise by a loose regex.
            rv = self.command("ATRV", wait=0.05)
            request, exchange = self._next_request()
            if not is_binary_reply(self.last_raw):
                volts = parse_atrv(rv)
                if volts is not None:
                    self._reading(out, "voltage", lambda b: b[0], [volts],
                                  request, exchange)
        if plan.dids_due(cycle):
            for (module, did), (key, fn) in DID_TABLE.items():
                data = self.request_did(did, module=module)
                request, exchange = self._next_request()
                if data:
                    self._reading(out, key, fn, data, request, exchange)
        if not full:
            self._cycle += 1
        return out

    # -- diagnostics -------------------------------------------------------- #
    # Addressing for the OBD service modes (decided from this codebase):
    #   03/07/0A and 04 go FUNCTIONAL (7DF). The DTC parser is built for
    #   multi-ECU replies (it de-dups across responders), the UI promises to
    #   read and clear ALL codes, and J1979 defines these as functional
    #   requests; ECM and TCM both answer 7DF on this truck. On the old
    #   leftover 7E0 header they only ever reached the ECM.
    #   0101 readiness goes PHYSICAL to the ECM (7E0): with headers off a
    #   functional reply interleaves ECUs, and the first '41 01' found could
    #   be the TCM's. The ECM owns the emissions monitors.
    DTC_HEADER = "7DF"
    READINESS_HEADER = "7E0"
    CLEAR_ACK_FROM = "7E8"      # the ECM must positively acknowledge a clear

    def _prepare(self, header: str, headers_on: bool) -> Optional[str]:
        """Confirm header, header display and compact formatting. Returns
        None when ready, else the reason the request must not go out."""
        if not self._set_header(header):
            return f"GT refused ATSH{header}; request not sent"
        if not self._at_checked("ATH1" if headers_on else "ATH0"):
            return "GT refused the header-display setting; request not sent"
        if not self._at_checked("ATS0"):
            return "GT refused ATS0; request not sent"
        # CAF1 is the ATZ default and nothing in this codebase turns it off,
        # but the frame parser depends on it (PCI shown, one frame per line,
        # adapter-driven flow control) — confirm it rather than assume it.
        if not self._at_checked("ATCAF1"):
            return "GT refused ATCAF1; request not sent"
        return None

    def _restore_default(self) -> None:
        """Back to the polling state; each step checked (poll_once repairs a
        failed header restore before trusting addressing again)."""
        self._set_header("7E0")
        self._at_checked("ATH0")

    #: Mode 09 goes PHYSICAL to the ECM: with headers off a functional
    #: request would interleave the TCM's and 7EB's CALID replies (they all
    #: answer 0904 on this truck), and the VIN that matters is the ECM's.
    IDENTITY_HEADER = "7E0"

    def _read_mode09(self, info_type: str) -> dict:
        """One physical Mode 09 request, parsed (parse_mode09_reply)."""
        resp = self.command("09" + info_type, wait=0.0)
        if is_binary_reply(self.last_raw):
            return {"examined": False,
                    "why": "the GT answered in its binary mode"}
        if b">" not in self.last_raw:
            return {"examined": False, "why": "no reply from the GT (no prompt)"}
        return parse_mode09_reply(resp, info_type)

    def _identity_ready(self) -> Optional[str]:
        """None when a Mode 09 request may go out, else why not."""
        if not self.ser:
            return "the GT link is not open"
        if self._header != self.IDENTITY_HEADER and not self._set_header(
                self.IDENTITY_HEADER):
            return f"GT refused ATSH{self.IDENTITY_HEADER}; request not sent"
        return None

    def read_vin(self) -> dict:
        """The VIN alone (one 0902 request): the identity entry
        read_vehicle_identity returns under "vin"."""
        why = self._identity_ready()
        parsed = ({"examined": False, "why": why} if why
                  else self._read_mode09("02"))
        return decode_vehicle_identity({"vin": parsed})["vin"]

    def read_vehicle_identity(self, vin: Optional[dict] = None) -> dict:
        """VIN (09 02), CALIDs (09 04) and CVNs (09 06) from the ECM.

        Returns {"examined": bool, "header": "7E0", "vin": {...}, "calids":
        {...}, "cvns": {...}}. Each entry is either {"examined": True,
        "raw": message hex, ...decoded...} or {"examined": False, "why":
        ...} (with "raw" when an answer was there but did not decode): an
        unanswered request is never an empty string pretending to be a
        value. Uses the polling state (7E0, ATH0) and leaves it in force.
        vin: an examined VIN entry already read on this connection (read_vin
        at connect); 0902 is then not asked again."""
        out: dict = {"examined": False, "header": self.IDENTITY_HEADER}
        parsed: dict = {}
        why = self._identity_ready()
        for info_type, (name, _size) in MODE09_ITEMS.items():
            if name == "vin" and vin is not None and vin.get("examined"):
                continue                       # read at connect, kept
            parsed[name] = ({"examined": False, "why": why} if why
                            else self._read_mode09(info_type))
        out.update(decode_vehicle_identity(parsed))
        if vin is not None and vin.get("examined"):
            out["vin"] = vin
        out["examined"] = any(out[n].get("examined")
                              for n, _s in MODE09_ITEMS.values())
        return out

    def read_dtcs(self) -> dict:
        """Stored / pending / permanent DTCs (modes 03 / 07 / 0A), functional.

        Returns {"examined": bool, "error": str|None, "stored": list|None,
        "pending": ..., "permanent": ..., "incomplete": {kind: {id: why}},
        "by_module": {kind: {id: [codes]}}}. A kind is None (NOT EXAMINED)
        when its request could not be addressed or no responder gave a
        complete positive answer — silence is never reported as 'no codes'.
        A kind can hold codes AND an incomplete entry: some modules read
        completely, others not; the UI must show both."""
        out: dict = {"examined": False, "error": None, "stored": None,
                     "pending": None, "permanent": None,
                     "incomplete": {}, "by_module": {}}
        try:
            # headers ON: multi-frame replies are reassembled per responder
            why = self._prepare(self.DTC_HEADER, headers_on=True)
            if why:
                out["error"] = why
                return out
            for mode, key in (("03", "stored"), ("07", "pending"),
                              ("0A", "permanent")):
                resp = self.command(mode, wait=0.15)
                if is_binary_reply(self.last_raw) or "?" in resp:
                    continue                  # no usable answer: unknown
                res = parse_dtc_reply(resp, mode)
                if res["incomplete"]:
                    out["incomplete"][key] = res["incomplete"]
                if res["by_module"]:
                    out["by_module"][key] = res["by_module"]
                    out[key] = res["codes"]
            out["examined"] = any(out[k] is not None for k in
                                  ("stored", "pending", "permanent"))
            if not out["examined"]:
                out["error"] = "no module answered the DTC requests"
            return out
        finally:
            self._restore_default()

    def read_module_dtcs(self, req_id: str, resp_id: str,
                         uds: bool = False) -> dict:
        """DTCs from ONE module, PHYSICALLY addressed (tx header = req_id, reply
        expected on resp_id), with the header restored to the ECM (7E0) after.

        uds=False: OBD modes 03/07/0A (2-byte DTCs) — the ISO15765 powertrain
        modules (ECM 7E0/7E8, TCM 7E2/7EA). uds=True: UDS $19 02 FF (3-byte DTC
        + status) — the GMLAN chassis/body modules (EBCM 243/643, BCM 241/641)
        which do NOT answer the functional OBD broadcast, so the functional
        read_dtcs() cannot see them.

        Returns {"examined": bool, "error": str|None, "codes": {...}, "raw":
        {...}}. For OBD: codes = {kind: [codes]} filtered to this module's
        resp_id. For UDS: codes = {"dtcs": [codes]}. Silence is reported as
        'not examined', never as 'no codes'."""
        target = req_id.upper()
        want = resp_id.upper()
        out: dict = {"examined": False, "error": None, "codes": {}, "raw": {}}
        why = self._prepare(target, headers_on=True)
        if why:
            out["error"] = why
            self._restore_default()
            return out
        try:
            if uds:
                resp = self.command("1902FF", wait=0.3)
                out["raw"]["1902"] = resp
                if is_binary_reply(self.last_raw) or "?" in resp:
                    out["error"] = "no usable answer to $19 02"
                else:
                    r = parse_uds19_reply(resp, want)
                    out["codes"]["dtcs"] = r["codes"]
                    out["examined"] = r["examined"]
                    if r["errors"]:
                        out["error"] = "; ".join(r["errors"])
                    elif not r["examined"]:
                        out["error"] = ("no 59 02 reply — the module may use a "
                                        "GM legacy DTC service, not UDS $19")
            else:
                for mode, key in (("03", "stored"), ("07", "pending"),
                                  ("0A", "permanent")):
                    resp = self.command(mode, wait=0.15)
                    out["raw"][key] = resp
                    if is_binary_reply(self.last_raw) or "?" in resp:
                        continue
                    res = parse_dtc_reply(resp, mode)
                    out["codes"][key] = res["by_module"].get(want, [])
                    out["examined"] = True
            if not out["examined"] and not out["error"]:
                out["error"] = "module did not answer the DTC request"
            return out
        finally:
            self._restore_default()

    def _long_listen(self) -> bool:
        """Keep the adapter listening through a multi-frame $A9 table. An
        ELM-style adapter ends reception (and prints '>') once no frame has
        arrived for its timeout, ~200 ms by default with adaptive timing; a
        module that pauses longer mid-table loses everything after the pause.
        ATAT0 + ATSTFF fixes the wait at the maximum (~1 s). Returns False if
        the GT refused either; the completeness check still catches a cut."""
        return self._at_checked("ATAT0") and self._at_checked("ATSTFF")

    def _restore_listen(self) -> None:
        self._at_checked("ATST32")            # ELM default timeout (~200 ms)
        self._at_checked("ATAT1")             # adaptive timing back on

    def read_gmlan_dtcs(self, req_id: str, sw: bool = False,
                        uudt_id: Optional[str] = None,
                        usdt_id: Optional[str] = None) -> dict:
        """DTCs from a GMLAN chassis/body module (EBCM 0x243, BCM 0x241) via GM
        service $A9 81, which they answer instead of UDS $19. Raw-CAN mode:
        select the raw HS (or SW) protocol, turn CAF off, set narrow receive
        filters for the module's UUDT report id (req+0x300) and USDT negative id
        (req+0x400), send 03 A9 81 FF (padded), capture the report frames, then
        RESTORE automatic protocol search + CAF. Verified sequence ported from
        truck-mcp (read_chassis_dtcs), which reads this truck's C0035 live.

        Returns {"examined", "error", "codes": {"dtcs": [faults], "table":
        [every reported code]}, "records", "raw"}. "dtcs" holds only codes whose
        status marks a fault (see parse_a9_report); the rest of the module's
        supported-DTC table is in "table" and is NOT a fault list. Needs the STN/OBDX ST commands (STP/STFAP); if the GT rejects
        STP the error says so."""
        req = int(req_id, 16)
        # 24x chassis ids report on req+0x300 and NAK on req+0x400; the 7Ex
        # powertrain-block ids do NOT follow that rule (TCCM 7E4 reports on
        # 0x5EC, NAKs on 7EC), so the caller passes them explicitly.
        uudt = (uudt_id or f"{req + 0x300:03X}").upper()
        usdt = (usdt_id or f"{req + 0x400:03X}").upper()
        out: dict = {"examined": False, "error": None,
                     "codes": {"dtcs": [], "table": []}, "records": [],
                     "raw": "", "complete": False}
        if sw:
            out["error"] = "SW-CAN GMLAN not supported over the GT's ELM yet"
            self._restore_default()
            return out
        # The OBDX Pro GT is NOT an STN device (it rejects STP), so the raw read
        # is done with plain ELM327 commands: force CAN 11/500 (the HS bus), turn
        # CAF off for raw framing, set the tx header, and widen the receive filter
        # to the module's non-standard report id (req+0x300) with ATCRA. The full
        # response is always kept in out["raw"] so a first truck run reveals the
        # exact frame shape even if the decode misses — bring-up over inference.
        if not self._at_checked("ATSP6"):     # ISO 15765-4 CAN 11-bit 500k
            out["error"] = "GT rejected ATSP6 (CAN 11/500)"
            self._restore_default()
            return out
        try:
            self._at_checked("ATCAF0")        # raw framing (supply PCI ourselves)
            if not self._set_header(req_id.upper()):
                out["error"] = f"could not set header {req_id}"
                return out
            self.command(f"ATCRA{uudt}", wait=0.05)   # accept the report id
            listen_ok = self._long_listen()
            resp = self.command("03A981FF55555555", wait=0.0, deadline=6.0)
            out["raw"] = resp
            if is_binary_reply(self.last_raw):
                out["error"] = "binary reply"
                return out
            p = parse_a9_report(resp, uudt_id=uudt)
            # a negative comes on the USDT id — look there only if the report id
            # was silent, so a clean report is never masked by a stray frame.
            if not p["examined"]:
                self.command(f"ATCRA{usdt}", wait=0.05)
                resp2 = self.command("03A981FF55555555", wait=0.0, deadline=3.0)
                out["raw"] = f"{resp} || {resp2}"
                pn = parse_a9_report(resp2, uudt_id=usdt)
                if pn["negative"]:
                    p["negative"] = pn["negative"]
            out["codes"]["dtcs"] = p["codes"]          # faults only
            out["codes"]["table"] = p["table"]         # every supported entry
            out["records"] = p["records"]
            out["examined"] = p["examined"]
            out["complete"] = p["complete"]
            if p["negative"]:
                out["error"] = f"module negative response (NRC {p['negative']})"
            elif p["examined"] and not p["complete"]:
                out["error"] = ("INCOMPLETE: no end-of-table marker, the capture "
                                "was cut short" + ("" if listen_ok else
                                " (GT refused the long-listen setting)"))
            elif not p["examined"]:
                out["error"] = ("no $A9 report decoded — raw kept for bring-up: "
                                + (out["raw"][:160] or "(nothing returned)"))
            return out
        finally:
            # leave the GT as the rest of the app expects.
            self._restore_listen()
            self.command("ATCRA", wait=0.05)  # reset the receive filter
            self._at_checked("ATCAF1")
            self.command("ATSP0", wait=0.1)   # back to automatic protocol search
            self._restore_default()

    def sweep_gmlan_dtcs(self) -> dict:
        """FUNCTIONAL GM $A9 81 to every HS-GMLAN node at once (AllNodes 0x101,
        extended address FE — the framing e38flash verified on this truck for
        its 0x101 $04 clear), then list EVERY id that answered. Read-only: $A9
        reads DTC tables, nothing is cleared or written.

        This finds modules by their answer instead of by guessing an address:
        each node reports on its own UUDT id (0x5xx) and NAKs on its USDT id
        (0x6xx), so a responder outside the identified set is a module the
        per-address scan cannot see. Two captures (receive filter 0x5xx, then
        0x6xx) because one 11-bit mask cannot pass both ranges.

        Returns {"examined", "error", "responders": {uudt_id: parse_a9_report},
        "negatives": {usdt_id: nrc}, "raw"}."""
        out: dict = {"examined": False, "error": None, "responders": {},
                     "negatives": {}, "raw": ""}
        req = "FE03A981FF555555"
        if not self._at_checked("ATSP6"):
            out["error"] = "GT rejected ATSP6 (CAN 11/500)"
            self._restore_default()
            return out
        try:
            for cmd in ("ATCAF0", "ATH1", "ATS0"):
                if not self._at_checked(cmd):
                    out["error"] = f"GT refused {cmd}; sweep not sent"
                    return out
            if not self._set_header("101"):
                out["error"] = "could not set header 101"
                return out
            self._long_listen()
            raws = []
            for filt in ("500", "600"):
                self.command("ATCRA", wait=0.05)
                if not (self._at_checked(f"ATCF{filt}")
                        and self._at_checked("ATCM700")):
                    out["error"] = f"GT refused the 0x{filt} receive filter"
                    return out
                resp = self.command(req, wait=0.0, deadline=6.0)
                if is_binary_reply(self.last_raw):
                    out["error"] = "binary reply"
                    return out
                raws.append(resp)
            out["raw"] = " || ".join(raws)
            ids: list = []
            for tok in raws[0].upper().split():
                c = "".join(ch for ch in tok if ch in "0123456789ABCDEF")
                if len(c) >= 13 and c[0] == "5" and c[3:5] == "81"                         and c[:3] not in ids:
                    ids.append(c[:3])
            for cid in ids:
                out["responders"][cid] = parse_a9_report(raws[0], uudt_id=cid)
            for tok in raws[1].upper().split():
                c = "".join(ch for ch in tok if ch in "0123456789ABCDEF")
                if len(c) >= 11 and c[0] == "6" and c[5:9] == "7FA9":
                    out["negatives"][c[:3]] = c[9:11]
            out["examined"] = bool(ids or out["negatives"])
            if not out["examined"]:
                out["error"] = ("no node answered the functional $A9 — raw kept "
                                "for bring-up: " + (out["raw"][:160] or "(nothing)"))
            return out
        finally:
            self._restore_listen()
            self.command("ATCRA", wait=0.05)
            self.command("ATCF000", wait=0.05)
            self.command("ATCM000", wait=0.05)
            self._at_checked("ATCAF1")
            self.command("ATSP0", wait=0.1)
            self._restore_default()

    def clear_dtcs(self) -> dict:
        """Mode 04, functional — clears codes AND readiness monitors in every
        emissions ECU that accepts it. The CALLER must have confirmed with the
        user (diagui.clear_codes does, default Cancel).

        Refuses to send 04 at all unless the functional header, header display
        and formatting are confirmed: a clear on an unverified header could
        hit the wrong module or 'succeed' without reaching the ECM.
        Returns {"sent": bool, "cleared": bool, "acked_by": [ids],
        "rejected_by": {id: nrc}, "error": str|None}. cleared needs a positive
        44 frame FROM the ECM (7E8) — not a '44' anywhere in the reply."""
        out: dict = {"sent": False, "cleared": False, "acked_by": [],
                     "rejected_by": {}, "error": None}
        try:
            why = self._prepare(self.DTC_HEADER, headers_on=True)
            if why:
                out["error"] = "clear refused: " + why
                return out
            out["sent"] = True
            resp = self.command("04", wait=0.4)
            if is_binary_reply(self.last_raw):
                out["error"] = "binary reply to 04 — result unknown"
                return out
            for can_id, payload in parse_frames(resp):
                if payload.startswith("44"):
                    out["acked_by"].append(can_id)
                elif payload.startswith("7F04") and len(payload) >= 6:
                    out["rejected_by"][can_id] = payload[4:6]
            out["cleared"] = self.CLEAR_ACK_FROM in out["acked_by"]
            if not out["cleared"]:
                nrc = out["rejected_by"].get(self.CLEAR_ACK_FROM)
                out["error"] = (f"ECM rejected the clear (NRC {nrc})" if nrc
                                else "no positive 44 from the ECM (7E8)")
            return out
        finally:
            self._restore_default()

    def readiness(self) -> dict:
        """PID 0101 from the ECM (physical 7E0). Returns parse_readiness()'s
        dict, or {"error": ...} when it could not be examined."""
        why = self._prepare(self.READINESS_HEADER, headers_on=False)
        if why:
            self._restore_default()
            return {"error": why}
        resp = self.request_raw("0101")
        if is_binary_reply(self.last_raw):
            return {"error": "binary reply to 0101"}
        data = self._extract(resp, "0101")
        out = parse_readiness(data or [])
        return out if out else {"error": "ECM did not answer 0101"}

    def scan_network(self) -> dict:
        """One pass over the comms pipeline for the module map:
        interface -> DLC voltage -> HS functional broadcast. Returns the raw
        facts; vehnet.scan_pipeline() adds the per-module physical pings and
        vehnet.localize() renders the verdict.

        interface_alive requires a positively identified ELM/OBDX TEXT reply;
        binary_mode flags the GT stuck in its J2534 binary mode. dlc_volts is
        None when ATRV did not parse (unreadable) — that is not 'no voltage'.
        Broadcast responders are positive evidence only: the GT's ELM layer
        has been seen returning one line when several modules answer, so an
        id missing from hs_responders proves nothing."""
        facts = {"interface_alive": False, "binary_mode": False,
                 "interface_reply": "", "dlc_volts": None, "dlc_reply": "",
                 "hs_responders": set(), "bcast_verified": False,
                 "pinged": {}}
        ati = self.command("ATI", wait=0.1)
        if is_binary_reply(self.last_raw):
            facts["binary_mode"] = True
            facts["interface_reply"] = repr(self.last_raw)
            return facts
        at1 = self.command("AT@1", wait=0.1)
        if is_binary_reply(self.last_raw):
            facts["binary_mode"] = True
            facts["interface_reply"] = repr(self.last_raw)
            return facts
        facts["interface_reply"] = f"ATI={ati!r} AT@1={at1!r}"
        if not is_elm_identity(ati, at1):
            return facts
        facts["interface_alive"] = True

        rv = self.command("ATRV", wait=0.05)
        if is_binary_reply(self.last_raw):
            facts["dlc_reply"] = repr(self.last_raw)
        else:
            facts["dlc_reply"] = rv
            facts["dlc_volts"] = parse_atrv(rv)

        # Functional broadcast with headers visible. The tx header must be
        # 7DF: left at 7E0 (open() sets it for DIDs) the "broadcast" is a
        # physical ECM request and only 7E8 can ever answer.
        if not self._at_checked("ATH1"):
            return facts
        try:
            facts["bcast_verified"] = self._set_header("7DF")
            if facts["bcast_verified"]:
                resp = self.command("0100", wait=0.5)
                if not is_binary_reply(self.last_raw):
                    facts["hs_responders"] = parse_hs_responders(resp)
        finally:
            self._set_header("7E0")
            self._at_checked("ATH0")
        return facts

    def ping_module(self, req_id: str, resp_id: str) -> Optional[bool]:
        """Physically address one module with TesterPresent ($3E, GMLAN
        single byte); any reply frame from its response id counts (a negative
        response still proves the module is alive on the bus).

        Returns True (answered); False (the request provably went out on
        req_id with the receive filter on resp_id and nothing came back); or
        None — NOT EXAMINED: the GT refused the header/filter setup or the
        request itself, so silence would say nothing about the module."""
        try:
            if not (self._at_checked("ATH1")
                    and self._set_header(req_id)
                    and self._at_checked("ATCRA" + resp_id)):
                return None
            resp = self.command("3E", wait=0.25)
            if is_binary_reply(self.last_raw) or "?" in resp:
                return None
            return reply_has_frame_from(resp, resp_id)
        finally:
            # Restore the automatic receive filter and the ECM header; each
            # is checked. A failed header restore leaves _header None, which
            # poll_once / request_did repair (or refuse on) before trusting
            # addressing again.
            self._at_checked("ATCRA")
            self._set_header("7E0")
            self._at_checked("ATH0")

    # -- GM enhanced (mode 22) --------------------------------------------- #
    def request_did(self, did: str, module: Optional[str] = None) -> Optional[list]:
        """Mode 22 ReadDataByIdentifier. module = 11-bit tx header (e.g. '7E2')
        to physically address a non-default module; restored to ECM after.
        Returns None when the header cannot be confirmed — a reply to an
        unverified header could belong to a different module."""
        target = (module or "7E0").upper()
        if self._header != target and not self._set_header(target):
            if target != "7E0":
                self._set_header("7E0")
            return None
        exchange = None
        try:
            data = self._extract_did(self.command("22" + did, wait=0.0), did)
            exchange = self.last_exchange
        finally:
            if target != "7E0":
                self._set_header("7E0")
                # last_exchange must date the DID reply, not the restore.
                self.last_exchange = exchange
        return data

    @staticmethod
    def _extract_did(resp: str, did: str) -> Optional[list]:
        s2 = "".join(ch for ch in resp.upper() if ch in "0123456789ABCDEF")
        header = "62" + did.upper()
        idx = s2.find(header)
        if idx < 0:
            return None
        rest = s2[idx + len(header):]
        rest = rest[:len(rest) - (len(rest) % 2)]
        return [int(rest[i:i + 2], 16) for i in range(0, len(rest), 2)]
