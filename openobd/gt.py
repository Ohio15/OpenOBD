"""
gt.py -- OBDX Pro GT live transport (ELM327 v2.1 compatible) + OBD-II polling.

The OBDX Pro GT enumerates as a USB CDC virtual COM port (STM32, VID 0483 /
PID 5740) and speaks the ELM327 command set. This module is the CLI/transport
seam the dashboard's GtDataSource wraps:

    open() / close() / command(cmd) / request_raw(mode_pid)

plus poll_once() which reads the supported mode-01 PIDs and decodes them into
OpenOBD canonical channel keys (see logbin.CANONICAL) with GAUGE_SPECS units
(vss in mph, temps in F, fuel pressure in psi).

Read-only: only ELM AT config + OBD mode-01 requests are issued. No ECU writes.
"""
from __future__ import annotations

import re
import time
from typing import Optional

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pyserial optional until the GT path is used
    serial = None
    list_ports = None


def _u16(b):
    return b[0] * 256 + b[1]


# pid byte (mode 01) -> (canonical_key, decode(list[int]) -> float in gauge units)
PID_TABLE = {
    "04": ("load",         lambda b: b[0] * 100.0 / 255.0),        # %
    "05": ("ect",          lambda b: (b[0] - 40) * 9 / 5 + 32),    # F
    "06": ("stft",         lambda b: (b[0] - 128) * 100.0 / 128),  # %
    "07": ("ltft",         lambda b: (b[0] - 128) * 100.0 / 128),  # %
    "0A": ("fuel_press",   lambda b: b[0] * 3 * 0.1450377),        # kPa->psi
    "0B": ("map",          lambda b: float(b[0])),                 # kPa
    "0C": ("rpm",          lambda b: _u16(b) / 4.0),               # rpm
    "0D": ("vss",          lambda b: b[0] * 0.6213712),            # km/h->mph
    "0E": ("spark",        lambda b: b[0] / 2.0 - 64.0),           # deg
    "0F": ("iat",          lambda b: (b[0] - 40) * 9 / 5 + 32),    # F
    "10": ("maf",          lambda b: _u16(b) / 100.0),             # g/s
    "11": ("tps",          lambda b: b[0] * 100.0 / 255.0),        # %
    "1F": ("run_time",     lambda b: float(_u16(b))),              # s
    "2F": ("fuel_level",   lambda b: b[0] * 100.0 / 255.0),        # %
    "33": ("baro",         lambda b: float(b[0])),                 # kPa
    "42": ("voltage",      lambda b: _u16(b) / 1000.0),            # V
    "44": ("eq_ratio",     lambda b: _u16(b) * 2.0 / 65536.0),     # lambda
    "46": ("ambient",      lambda b: (b[0] - 40) * 9 / 5 + 32),    # F
    "49": ("app",          lambda b: b[0] * 100.0 / 255.0),        # %
    "4C": ("cmd_throttle", lambda b: b[0] * 100.0 / 255.0),        # %
    "52": ("ethanol",      lambda b: b[0] * 100.0 / 255.0),        # %
}

# canonical keys this transport can ever surface (for gauge pre-build)
CANONICAL_KEYS = sorted({v[0] for v in PID_TABLE.values()})

# ---- GM enhanced parameters (mode 22 ReadDataByIdentifier) ----------------- #
# Confirmed on this truck: the E38 ECM answers mode 22 (22 1940 -> 62 1940 28).
# The T43 TCM did NOT answer 7E1 in testing because it is not there: on this
# truck the TCM is 7E2 -> 7EA (see vehnet.MODULES for the capture evidence).
# Populate DID_TABLE once the GM DID -> parameter + scaling is known; entries
# are polled and merged into the sample exactly like PIDs. Left EMPTY on purpose
# so no unverified/guessed values ever reach the gauges.
#   key format:  (module_header_or_None, did_hex) : (canonical_key, decode(list[int]))
#   example:     (None, "1940"): ("some_engine_param", lambda b: b[0])
DID_TABLE = {
    # Trans fluid temp: correlated live to the DIC (194 F) on this truck.
    # ECM DID 1644, byte1, GM standard (byte-40) C -> F. Stable across
    # engine on/off and distinct from coolant; verify tracking on a drive.
    (None, "1644"): ("tft", lambda b: (b[1] - 40) * 9 / 5 + 32),
}
CANONICAL_KEYS = sorted(set(CANONICAL_KEYS) | {v[0] for v in DID_TABLE.values()})

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


def parse_dtc_response(resp: str, mode: str) -> list[str]:
    """Parse an ELM response to mode 03/07/0A into DTC strings. Handles CAN
    framing (count byte after the response mode) and multi-ECU replies by
    scanning every occurrence of the response-mode marker."""
    rmode = "%02X" % (int(mode, 16) + 0x40)
    s = _hexonly(resp)
    codes: list[str] = []
    idx = 0
    while True:
        i = s.find(rmode, idx)
        if i < 0:
            break
        rest = s[i + 2:]
        if len(rest) >= 2:
            n = int(rest[:2], 16)
            take = rest[2:2 + n * 4] if n <= 8 else ""
            for j in range(0, len(take) - 3, 4):
                b1, b2 = int(take[j:j + 2], 16), int(take[j + 2:j + 4], 16)
                if b1 or b2:
                    codes.append(format_dtc(b1, b2))
        idx = i + 2
    # de-dup preserving order (same code from multiple ECUs)
    seen: set[str] = set()
    return [c for c in codes if not (c in seen or seen.add(c))]


# Continuous + non-continuous monitors for spark-ignition, OBD-II PID 01.
_CONT_MONITORS = ["Misfire", "Fuel system", "Components"]
_SPARK_MONITORS = ["Catalyst", "Heated catalyst", "EVAP system",
                   "Secondary air", "A/C refrigerant", "O2 sensor",
                   "O2 heater", "EGR system"]


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
    def __init__(self, port: Optional[str] = None, baud: int = 115200,
                 timeout: float = 0.5):
        self.port_name = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None
        self.supported: set[str] = set()
        self.device = "?"
        # The tx header the GT is KNOWN to be using, or None when a header
        # command was refused and the real header is unknown. Requests whose
        # meaning depends on addressing must not go out while this is None.
        self._header: Optional[str] = None
        # Per-reply read deadline; a binary-mode GT never sends '>' so every
        # read in that state runs to this deadline.
        self.read_deadline_s = 1.2
        self.last_raw = b""

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
        self.ser = serial.Serial(self.port_name, self.baud, timeout=self.timeout)
        time.sleep(0.2)
        self.command("ATZ", wait=0.9)
        for c in ("ATE0", "ATL0", "ATS0", "ATH0", "ATSP0"):
            self.command(c, wait=0.2)
        try:
            self.device = self.command("AT@1", wait=0.2) or "OBDX Pro GT"
        except Exception:
            self.device = "OBDX Pro GT"
        self._probe_supported()
        self._set_header("7E0")  # physical ECM addr for mode-22 DIDs

    def close(self) -> None:
        if self.ser:
            try:
                self.ser.close()
            finally:
                self.ser = None

    # -- raw io ------------------------------------------------------------ #
    def command(self, cmd: str, wait: float = 0.0) -> str:
        """Send one command; returns the reply as flattened text. The raw
        bytes are kept in self.last_raw so callers can tell a binary-mode
        reply from ELM text (the lossy decode alone would hide it)."""
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.ser.write((cmd + "\r").encode())
        if wait:
            time.sleep(wait)
        return self._read_to_prompt()

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
        for base in ("0100", "0120", "0140", "0160"):
            resp = self.request_raw(base)
            data = self._extract(resp, base)
            if not data or len(data) < 4:
                break
            start = int(base[2:], 16)
            mask = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
            for bit in range(32):
                if mask & (1 << (31 - bit)):
                    self.supported.add("%02X" % (start + 1 + bit))
            if not (mask & 1):  # bit for "next range supported"
                break

    def poll_once(self) -> dict:
        out: dict[str, float] = {}
        if not self.ser:
            return out
        # Physical ECM addressing is what makes these values ECM values. If a
        # header command was refused earlier, re-establish it; if the GT still
        # refuses, report nothing rather than another module's bytes.
        if self._header != "7E0" and not self._set_header("7E0"):
            return out
        for pid, (key, fn) in PID_TABLE.items():
            if self.supported and pid not in self.supported:
                continue
            data = self._extract(self.request_raw("01" + pid), "01" + pid)
            if not data:
                continue
            try:
                out[key] = float(fn(data))
            except Exception:
                pass
        if "voltage" not in out:
            m = re.search(r"([\d.]+)\s*V", self.command("ATRV", wait=0.05))
            if m:
                out["voltage"] = float(m.group(1))
        for (module, did), (key, fn) in DID_TABLE.items():
            data = self.request_did(did, module=module)
            if not data:
                continue
            try:
                out[key] = float(fn(data))
            except Exception:
                pass
        return out

    # -- diagnostics -------------------------------------------------------- #
    def read_dtcs(self) -> dict:
        """Stored / pending / permanent DTCs (modes 03 / 07 / 0A)."""
        out = {}
        for mode, key in (("03", "stored"), ("07", "pending"),
                          ("0A", "permanent")):
            resp = self.command(mode, wait=0.15)
            out[key] = parse_dtc_response(resp, mode)
        return out

    def clear_dtcs(self) -> bool:
        """Mode 04 — clears codes AND readiness monitors. Caller confirms."""
        resp = self.command("04", wait=0.4)
        return "44" in _hexonly(resp)

    def readiness(self) -> dict:
        data = self._extract(self.request_raw("0101"), "0101")
        return parse_readiness(data or [])

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
        try:
            data = self._extract_did(self.command("22" + did, wait=0.0), did)
        finally:
            if target != "7E0":
                self._set_header("7E0")
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
