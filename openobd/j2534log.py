"""
j2534log.py -- offline decoder for a captured J2534 pass-thru trace.

PURPOSE AND SCOPE (read this before extending the module).

The OBDX Pro GT speaks ELM327 over USB-CDC for OpenOBD's own reads, but a GM
factory tool (Techline Connect / GDS2 / SPS) drives the device through its SAE
J2534 pass-thru DLL instead. Capturing that session -- with the Ron-run logging
proxy in ``tools/j2534_trace_proxy`` -- records every CAN/ISO-TP frame the
factory tool exchanges with the truck. This module turns that capture into
usable knowledge for OpenOBD's gauges.

The one open item on OpenOBD's live gauges is the mode-22 DID map: the E38 ECM
answers ReadDataByIdentifier, but the GM DID -> parameter + scaling map is
unknown, so ``gt.DID_TABLE`` was left empty rather than guessed (see its header
comment). A factory session reads dozens of those DIDs before it does anything
else. This decoder reassembles the ISO-TP frames, pairs each 0x22 request with
its 0x62 response, and emits a *candidate* DID table -- observed DIDs, the
module that answered, and the raw response bytes -- which you then correlate
against a known physical value before any entry goes into ``gt.DID_TABLE``.

FENCE (DR-011 boundary, deliberate and load-bearing). This decoder is a READER.
It classifies every service on the bus so a capture is fully accounted for, but
it synthesises reusable output ONLY for the read services (0x22, 0x01, 0x09,
0x19/0x03/0x07/0x0A, 0x21). For the programming / security services it sees --
SecurityAccess (0x27), RequestDownload/TransferData/TransferExit
(0x34/0x36/0x37), WriteDataByIdentifier (0x2E), RoutineControl (0x31), session
and reset control (0x10/0x11) -- it records that they occurred and which
identifiers they touched, and nothing else. It never emits seed/key material,
transfer payloads, or an ordered sequence that could serve as a flash/unlock
recipe. Reconstructing the programming path is explicitly out of scope and must
not be added here.

Pure-logic module: no device I/O, no third-party deps. Input is a normalised
list of frames produced by one of the parsers below, so the decoder is
independent of how the capture was taken.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


# --------------------------------------------------------------------------- #
# Service taxonomy
# --------------------------------------------------------------------------- #
# Request SID -> (positive-response SID, short name). GM uses both legacy OBD
# modes (0x01..0x0A) and UDS services (0x10+). A positive response SID is the
# request SID + 0x40; 0x7F is a negative response for any request.
_SERVICE_NAMES = {
    0x01: "OBD_currentData",
    0x02: "OBD_freezeFrame",
    0x03: "OBD_storedDTCs",
    0x04: "OBD_clearDTCs",
    0x06: "OBD_monitorResults",
    0x07: "OBD_pendingDTCs",
    0x09: "OBD_vehicleInfo",
    0x0A: "OBD_permanentDTCs",
    0x10: "UDS_diagnosticSessionControl",
    0x11: "UDS_ecuReset",
    0x14: "UDS_clearDiagnosticInformation",
    0x19: "UDS_readDTCInformation",
    0x1A: "GM_readDataByIdentifier_legacy",
    0x21: "GM_readDataByLocalId",
    0x22: "UDS_readDataByIdentifier",
    0x23: "UDS_readMemoryByAddress",
    0x27: "UDS_securityAccess",
    0x28: "UDS_communicationControl",
    0x2E: "UDS_writeDataByIdentifier",
    0x2F: "UDS_inputOutputControl",
    0x31: "UDS_routineControl",
    0x34: "UDS_requestDownload",
    0x35: "UDS_requestUpload",
    0x36: "UDS_transferData",
    0x37: "UDS_transferExit",
    0x3B: "GM_writeDataByLocalId",
    0x3E: "UDS_testerPresent",
    0x85: "UDS_controlDTCSetting",
    0xA2: "GM_reportProgrammingState",
    0xA5: "GM_proprietary_A5",
}

# Services whose payloads this module DECODES into reusable output. Everything
# else is observed and counted but never turned into a sequence (see FENCE).
_READ_SERVICES = frozenset({0x01, 0x03, 0x07, 0x09, 0x0A, 0x19, 0x21, 0x22, 0x1A})

# Services that carry or gate a write/flash/unlock. For these the decoder
# records occurrence and the identifier touched (DID / routine id), never the
# payload bytes or their order. This set exists to be reported, not executed.
_PROGRAMMING_SERVICES = frozenset(
    {0x10, 0x11, 0x27, 0x28, 0x2E, 0x2F, 0x31, 0x34, 0x35, 0x36, 0x37, 0x3B, 0x85}
)

# UDS negative-response codes seen often enough to name in a summary.
_NRC_NAMES = {
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x78: "requestCorrectlyReceived_responsePending",
    0x7F: "serviceNotSupportedInActiveSession",
}


def service_name(sid: int) -> str:
    """Human name for a request SID (positive-response SIDs map back to it)."""
    if sid in _SERVICE_NAMES:
        return _SERVICE_NAMES[sid]
    if sid >= 0x40 and (sid - 0x40) in _SERVICE_NAMES:
        return _SERVICE_NAMES[sid - 0x40] + "_response"
    return f"unknown_0x{sid:02X}"


# --------------------------------------------------------------------------- #
# Normalised frame
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Frame:
    """One CAN frame from the trace.

    ts        monotonic seconds (float) if the capture had timestamps, else the
              frame's ordinal as a float -- order is what the decoder relies on.
    direction "tx" (tester -> vehicle) or "rx" (vehicle -> tester). The proxy
              knows this from which J2534 call carried the frame
              (PassThruWriteMsgs vs PassThruReadMsgs).
    can_id    11- or 29-bit arbitration id (int).
    data      the CAN data field, 0..8 bytes for classic CAN (bytes).
    """
    ts: float
    direction: str
    can_id: int
    data: bytes


# --------------------------------------------------------------------------- #
# Trace parsers -> list[Frame]
# --------------------------------------------------------------------------- #
# The logging proxy emits JSONL (one object per frame) as its native format; a
# CSV form and a loose hex-line form are accepted too so a trace from any tool
# can be fed in after a trivial reshape.

def _coerce_id(v) -> int:
    if isinstance(v, int):
        return v
    s = str(v).strip().lower()
    return int(s, 16) if s.startswith("0x") else int(s, 16) if any(
        c in "abcdef" for c in s) else int(s)


def _coerce_data(v) -> bytes:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, (list, tuple)):          # JSONL data as a list of ints
        return bytes(int(x) & 0xFF for x in v)
    s = "".join(ch for ch in str(v).upper() if ch in "0123456789ABCDEF")
    if len(s) % 2:
        s = s[:-1]
    return bytes(int(s[i:i + 2], 16) for i in range(0, len(s), 2))


def _norm_dir(v) -> str:
    s = str(v).strip().lower()
    if s in ("tx", "w", "write", "out", "tester", "req", "request"):
        return "tx"
    if s in ("rx", "r", "read", "in", "ecu", "resp", "response"):
        return "rx"
    raise ValueError(f"unrecognised direction {v!r}")


def parse_jsonl(text: str) -> list[Frame]:
    """Native proxy format: one JSON object per line with keys
    ts, dir, id, data (data = hex string or list of ints)."""
    out: list[Frame] = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        o = json.loads(line)
        out.append(Frame(
            ts=float(o.get("ts", i)),
            direction=_norm_dir(o.get("dir", o.get("direction"))),
            can_id=_coerce_id(o.get("id", o.get("can_id"))),
            data=_coerce_data(o.get("data", "")),
        ))
    return out


def parse_csv(text: str) -> list[Frame]:
    """CSV with a header row containing at least ts,dir,id,data (any order)."""
    out: list[Frame] = []
    rdr = csv.DictReader(io.StringIO(text))
    if not rdr.fieldnames:
        return out
    cols = {c.strip().lower(): c for c in rdr.fieldnames}
    for key in ("dir", "id", "data"):
        if key not in cols and key not in (c[:len(key)] for c in cols):
            pass
    for i, row in enumerate(rdr):
        g = {k.strip().lower(): v for k, v in row.items()}
        out.append(Frame(
            ts=float(g.get("ts") or i),
            direction=_norm_dir(g.get("dir") or g.get("direction")),
            can_id=_coerce_id(g.get("id") or g.get("can_id")),
            data=_coerce_data(g.get("data") or ""),
        ))
    return out


def parse_hexlines(text: str) -> list[Frame]:
    """Loose format: 'dir id data' per line, e.g. 'tx 7E0 03221940'.
    Timestamp is the line ordinal. Blank and '#' lines ignored."""
    out: list[Frame] = []
    i = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        out.append(Frame(
            ts=float(i),
            direction=_norm_dir(parts[0]),
            can_id=_coerce_id(parts[1]),
            data=_coerce_data("".join(parts[2:])),
        ))
        i += 1
    return out


import re

# The OBDX Pro J2534 driver's own debug log (enabled by LoggingEnabled:1 in
# %APPDATA%\OBDX Pro\J2534\Settings\OBDXGT_Config.cfg; files land in the sibling
# Logs\ dir) is an alternative to the logging proxy -- no DLL to compile. OBDX
# does not document the line format, so this parser matches the STANDARD J2534
# debug-log convention and is deliberately tolerant:
#
#   * direction comes from the most recent PassThru call line -- a line naming
#     WriteMsgs / StartPeriodicMsg is "tx", one naming ReadMsgs is "rx"; an
#     explicit " TX "/" RX " token on the data line overrides.
#   * a data line carries the frame bytes after a "Data:" label, or is itself a
#     run of hex byte pairs. For ISO15765/CAN the first 4 bytes are the
#     arbitration id (big-endian) and the rest is the CAN data field.
#
# VALIDATE against the first real OBDX capture before trusting the output: if
# OBDX's labels differ, adjust _OBDX_* below. The decoder downstream is
# format-independent, so only this extractor is at risk.
_OBDX_TX = re.compile(r"writemsg|startperiodic|\btx\b", re.I)
_OBDX_RX = re.compile(r"readmsg|\brx\b", re.I)
_OBDX_TS = re.compile(r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,6})|\+?(\d+)\s*us", re.I)
_OBDX_HEXRUN = re.compile(r"(?:[0-9A-Fa-f]{2}[\s:]+){3,}[0-9A-Fa-f]{2}")


def _obdx_ts(line: str, ordinal: int) -> float:
    m = _OBDX_TS.search(line)
    if not m:
        return float(ordinal)
    if m.group(1):
        hh, mm, rest = m.group(1).replace(",", ".").split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(rest)
    return float(m.group(2)) / 1e6


def parse_obdx_log(text: str) -> list[Frame]:
    """Parse the OBDX Pro J2534 driver's native debug log (see note above).
    Tolerant by design; validate against a real capture."""
    out: list[Frame] = []
    cur_dir: Optional[str] = None
    ordinal = 0
    for line in text.splitlines():
        low = line.lower()
        # a function-call line sets the direction for the frames under it
        if "passthru" in low or _OBDX_TX.search(line) or _OBDX_RX.search(line):
            if _OBDX_RX.search(line) and "readmsg" in low:
                cur_dir = "rx"
            elif _OBDX_TX.search(line) and ("writemsg" in low or "periodic" in low):
                cur_dir = "tx"
        # does this line carry frame bytes?
        hexpart = None
        idx = low.rfind("data:")
        if idx >= 0:
            hexpart = line[idx + 5:]
        else:
            m = _OBDX_HEXRUN.search(line)
            if m:
                hexpart = m.group(0)
        if hexpart is None:
            continue
        line_dir = cur_dir
        if _OBDX_RX.search(line) and "readmsg" not in low:
            line_dir = "rx"
        elif _OBDX_TX.search(line) and "writemsg" not in low and "periodic" not in low:
            line_dir = "tx"
        if line_dir is None:
            continue
        data = _coerce_data(hexpart)
        if len(data) < 4:
            continue
        out.append(Frame(ts=_obdx_ts(line, ordinal), direction=line_dir,
                         can_id=(data[0] << 24) | (data[1] << 16) |
                                (data[2] << 8) | data[3],
                         data=data[4:]))
        ordinal += 1
    return out


def parse_trace(text: str, fmt: str = "auto") -> list[Frame]:
    """Parse a trace in jsonl / csv / hexlines / obdx. 'auto' sniffs: '{' ->
    jsonl; a PassThru/Data: line -> obdx; a comma + dir/id/data header -> csv;
    else hexlines."""
    if fmt != "auto":
        return {"jsonl": parse_jsonl, "csv": parse_csv,
                "hexlines": parse_hexlines, "obdx": parse_obdx_log}[fmt](text)
    low = text.lower()
    if re.search(r"passthru(read|write)msgs|loggingenabled", low):
        return parse_obdx_log(text)
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("{"):
            return parse_jsonl(text)
        if "," in s and any(h in s.lower() for h in ("dir", "id", "data")):
            return parse_csv(text)
        return parse_hexlines(text)
    return []


# --------------------------------------------------------------------------- #
# ISO-TP reassembly over frames
# --------------------------------------------------------------------------- #
# Same error model as gt.reassemble_isotp (which works on ELM text tokens):
# a single frame shorter than its length, a first frame before the previous
# message completed, a sequence gap, or a message that never reaches its
# declared length is an ERROR for that id -- never a silently short payload.
# This variant works on binary CAN frames (PCI in data[0]) and keeps each
# arbitration id's reassembly separate, so interleaved responders are fine.

@dataclass
class IsoTpMessage:
    can_id: int
    direction: str
    payload: bytes
    ts_first: float
    ts_last: float
    error: Optional[str] = None


def reassemble(frames: Iterable[Frame]) -> list[IsoTpMessage]:
    """Reassemble ISO-TP messages, preserving completion order. A frame with
    fewer than 1 data byte is skipped; flow-control (PCI 0x3) is skipped but
    does not break an in-flight message."""
    pending: dict[int, dict] = {}
    out: list[IsoTpMessage] = []

    def emit(cid, direction, payload, t0, t1, err=None):
        out.append(IsoTpMessage(cid, direction, bytes(payload), t0, t1, err))

    for f in frames:
        d = f.data
        if len(d) < 1:
            continue
        cid = f.can_id
        pci = (d[0] >> 4) & 0xF
        st = pending.get(cid)

        if pci == 0x0:  # single frame
            length = d[0] & 0xF
            if st is not None:
                emit(cid, st["dir"], st["buf"][:st["len"]], st["t0"], f.ts,
                     "new single frame before the previous message completed")
                pending.pop(cid, None)
            body = d[1:1 + length]
            if length == 0 or len(body) < length:
                emit(cid, f.direction, body, f.ts, f.ts,
                     f"single frame shorter than its length {length}")
                continue
            emit(cid, f.direction, body, f.ts, f.ts)

        elif pci == 0x1:  # first frame
            if len(d) < 2:
                emit(cid, f.direction, b"", f.ts, f.ts,
                     "first frame without a length")
                pending.pop(cid, None)
                continue
            length = ((d[0] & 0xF) << 8) | d[1]
            if st is not None:
                emit(cid, st["dir"], st["buf"][:st["len"]], st["t0"], f.ts,
                     "new first frame before the previous message completed")
            pending[cid] = {"len": length, "buf": bytearray(d[2:]),
                            "sn": 1, "dir": f.direction, "t0": f.ts}

        elif pci == 0x2:  # consecutive frame
            if st is None:
                emit(cid, f.direction, d[1:], f.ts, f.ts,
                     f"consecutive frame {d[0] & 0xF} without a first frame")
                continue
            sn = d[0] & 0xF
            if sn != st["sn"]:
                emit(cid, st["dir"], st["buf"][:st["len"]], st["t0"], f.ts,
                     f"sequence gap: expected frame {st['sn']}, got {sn}")
                pending.pop(cid, None)
                continue
            st["buf"].extend(d[1:])
            st["sn"] = (st["sn"] + 1) & 0xF

        else:  # 0x3 flow control, or anything else -- not payload
            continue

        st = pending.get(cid)
        if st is not None and len(st["buf"]) >= st["len"]:
            emit(cid, st["dir"], st["buf"][:st["len"]], st["t0"], f.ts)
            pending.pop(cid, None)

    for cid, st in pending.items():
        emit(cid, st["dir"], st["buf"][:st["len"]], st["t0"], st["t0"],
             f"truncated: {len(st['buf'])} of {st['len']} bytes")
    return out


# --------------------------------------------------------------------------- #
# UDS/OBD decode + pairing
# --------------------------------------------------------------------------- #
def _resp_id_for(tx_id: int) -> Optional[int]:
    """GM 11-bit convention: a physical request on 7E0..7E7 is answered on
    7E8..7EF. 7DF is functional broadcast (any 7E8..7EF may answer)."""
    if 0x7E0 <= tx_id <= 0x7E7:
        return tx_id + 0x08
    return None


@dataclass
class DidObservation:
    module: int          # the request (tx) arbitration id
    did: str             # 4-hex DID
    data: bytes          # response bytes AFTER the 0x62 + 2 DID bytes
    count: int = 1
    ts_first: float = 0.0


@dataclass
class ServiceStat:
    sid: int
    name: str
    requests: int = 0
    positive: int = 0
    negative: int = 0
    nrcs: dict = field(default_factory=dict)        # nrc -> count
    identifiers: set = field(default_factory=set)   # DIDs / routine ids touched


@dataclass
class DecodeResult:
    did_observations: dict          # (module, did) -> DidObservation
    services: dict                  # sid -> ServiceStat
    programming_seen: bool
    messages: int
    errors: list                    # (can_id, reason)

    def candidate_did_rows(self) -> list[tuple[str, str, str]]:
        """Rows for a candidate table: (did, module_hex, response_hex),
        sorted by DID then module. These are OBSERVED read DIDs; each still
        needs correlation before it enters gt.DID_TABLE."""
        rows = []
        for (mod, did), o in self.did_observations.items():
            rows.append((did, f"{mod:03X}", o.data.hex().upper()))
        return sorted(rows)


def _ident_hex(service: int, req_payload: bytes) -> Optional[str]:
    """The identifier a service addresses, for reporting WHICH id was touched
    without recording the payload. DID services carry a 2-byte id after the
    SID; RoutineControl carries a sub-function then a 2-byte routine id."""
    if service in (0x22, 0x2E) and len(req_payload) >= 3:
        return req_payload[1:3].hex().upper()
    if service == 0x31 and len(req_payload) >= 4:
        return req_payload[2:4].hex().upper()
    if service in (0x01, 0x21, 0x19) and len(req_payload) >= 2:
        return f"{req_payload[1]:02X}"
    return None


def decode(frames: Iterable[Frame]) -> DecodeResult:
    """Decode a frame list into read-DID observations and a per-service
    summary. Requests (tx) are paired to the next response (rx) on the paired
    arbitration id; a 0x62 reply yields a DID observation."""
    msgs = reassemble(frames)
    services: dict[int, ServiceStat] = {}
    dids: dict[tuple[int, str], DidObservation] = {}
    errors: list[tuple[int, str]] = []
    programming = False

    # index responses by (can_id) in arrival order for pairing
    responses = [m for m in msgs if m.direction == "rx" and not m.error]
    used = [False] * len(responses)

    def stat(sid: int) -> ServiceStat:
        s = services.get(sid)
        if s is None:
            s = services[sid] = ServiceStat(sid, service_name(sid))
        return s

    for m in msgs:
        if m.error:
            errors.append((m.can_id, m.error))
            continue
        if m.direction != "tx" or not m.payload:
            continue
        sid = m.payload[0]
        s = stat(sid)
        s.requests += 1
        if sid in _PROGRAMMING_SERVICES:
            programming = True
        ident = _ident_hex(sid, m.payload)
        if ident:
            s.identifiers.add(ident)

        # find the paired response: same-or-later, on the response id, matching
        # service (positive 0x40+sid, or 0x7F negative naming this sid).
        resp_id = _resp_id_for(m.can_id)
        for j, r in enumerate(responses):
            if used[j] or r.ts_last < m.ts_first:
                continue
            if resp_id is not None and r.can_id != resp_id:
                continue
            if not r.payload:
                continue
            rsid = r.payload[0]
            if rsid == sid + 0x40:
                used[j] = True
                s.positive += 1
                if sid == 0x22 and len(m.payload) >= 3 and len(r.payload) >= 3:
                    did = m.payload[1:3].hex().upper()
                    key = (m.can_id, did)
                    o = dids.get(key)
                    if o is None:
                        dids[key] = DidObservation(
                            module=m.can_id, did=did, data=r.payload[3:],
                            ts_first=m.ts_first)
                    else:
                        o.count += 1
                break
            if rsid == 0x7F and len(r.payload) >= 2 and r.payload[1] == sid:
                used[j] = True
                s.negative += 1
                nrc = r.payload[2] if len(r.payload) >= 3 else -1
                s.nrcs[nrc] = s.nrcs.get(nrc, 0) + 1
                break

    return DecodeResult(
        did_observations=dids,
        services=dict(sorted(services.items())),
        programming_seen=programming,
        messages=len([m for m in msgs if not m.error]),
        errors=errors,
    )


# --------------------------------------------------------------------------- #
# Scaling correlation (suggestion only -- never auto-committed to DID_TABLE)
# --------------------------------------------------------------------------- #
# Candidate byte->value transforms. These mirror the shapes already in
# gt.DID_TABLE / PID_TABLE (byte0, byte1, big/little-endian 16-bit, with an
# optional linear scale+offset). The helper suggests which transform maps a
# DID's observed bytes to a known physical value; a human still confirms it on
# the truck before the entry goes live, exactly as DID_TABLE's header requires.

def _candidate_transforms() -> list[tuple[str, Callable[[bytes], Optional[float]]]]:
    def g(i):
        return lambda b: float(b[i]) if len(b) > i else None

    def be16(i):
        return lambda b: float((b[i] << 8) | b[i + 1]) if len(b) > i + 1 else None

    def le16(i):
        return lambda b: float(b[i] | (b[i + 1] << 8)) if len(b) > i + 1 else None

    out = [("byte0", g(0)), ("byte1", g(1)), ("byte2", g(2)),
           ("be16@0", be16(0)), ("le16@0", le16(0)),
           ("be16@1", be16(1))]
    return out


# Common GM linear fits (scale, offset) applied after the raw transform:
# C->F temp (byte-40 then *9/5+32), kPa, %, rpm (*0.25), spark (-64 then *0.5).
_LINEAR_FITS = {
    "identity": (1.0, 0.0),
    "temp_C_minus40": (1.0, -40.0),
    "temp_F_from_C": (9.0 / 5.0, 32.0 - 40.0 * 9.0 / 5.0),  # (raw-40)*9/5+32
    "rpm_x0.25": (0.25, 0.0),
    "pct_x100/255": (100.0 / 255.0, 0.0),
    "spark_half_minus64": (0.5, -64.0),
}


def suggest_scaling(raw: bytes, known_value: float,
                    tol: float = 1.0) -> list[dict]:
    """Suggest (transform, fit) pairs whose output is within ``tol`` of
    ``known_value`` for the observed raw bytes. Returns the matches sorted by
    closeness. A suggestion is a lead for a human to confirm, not a decode to
    trust -- see the DID_TABLE header in gt.py."""
    hits = []
    for tname, tf in _candidate_transforms():
        base = tf(raw)
        if base is None:
            continue
        for fname, (scale, offset) in _LINEAR_FITS.items():
            val = base * scale + offset
            err = abs(val - known_value)
            if err <= tol:
                hits.append({"transform": tname, "fit": fname,
                             "value": round(val, 3), "error": round(err, 3)})
    return sorted(hits, key=lambda h: h["error"])
