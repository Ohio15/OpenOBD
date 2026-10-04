"""
j2534.py -- SAE J2534 (v04.04) pass-thru client for the OBDX Pro GT.

This is the full-capability transport: unlike the ELM327 serial mode (HS-CAN
only), J2534 reaches every GM bus the device supports -- including GM
single-wire CAN (SW_CAN / GMLAN, 33.3 kbps) where the T43 TCM lives. It also
lays the groundwork for the (gated) flashing path.

Pure ctypes over the registered PassThruSupport.04.04 DLL. No third-party deps.
"""
from __future__ import annotations

import ctypes
from ctypes import (c_ulong, c_long, c_void_p, POINTER, byref, create_string_buffer,
                    Structure)
from typing import Optional

# --------------------------------------------------------------------------- #
# Constants (SAE J2534 v04.04 + J2534-2 pin-switched protocols)
# --------------------------------------------------------------------------- #
# Base protocol IDs
CAN = 5
ISO15765 = 6
J1850VPW = 1
# Pin-switched (J2534-2) protocol IDs
J1850VPW_PS      = 0x00008000
CAN_PS           = 0x00008004
ISO15765_PS      = 0x00008005
SW_ISO15765_PS   = 0x00008007  # single-wire ISO-TP (GMLAN) -- confirm at runtime
SW_CAN_PS        = 0x00008008  # single-wire raw CAN (GMLAN) -- confirm at runtime
GM_UART_PS       = 0x00008009

# Connect / TxFlags
CAN_29BIT_ID        = 0x00000100
ISO15765_FRAME_PAD  = 0x00000040

# Filter types
PASS_FILTER          = 1
BLOCK_FILTER         = 2
FLOW_CONTROL_FILTER  = 3

# Ioctl IDs
GET_CONFIG        = 0x01
SET_CONFIG        = 0x02
READ_VBATT        = 0x03
CLEAR_TX_BUFFER   = 0x07
CLEAR_RX_BUFFER   = 0x08
CLEAR_PERIODIC    = 0x09
CLEAR_MSG_FILTERS = 0x0A

# SCONFIG parameter IDs (subset)
DATA_RATE               = 0x01
LOOPBACK                = 0x03
ISO15765_BS             = 0x1E
ISO15765_STMIN          = 0x1F
SW_CAN_HS_DATA_RATE     = 0x11
SW_CAN_SPEEDCHANGE_ENABLE = 0x12
SW_CAN_RES_SWITCH       = 0x13
J1962_PINS              = 0x8001    # J2534-2: pin selection for _PS protocols
# J1962_PINS value: (pin << 8) | second pin. GM single-wire CAN is pin 1 alone.
SW_CAN_PINS             = 0x0100
SW_CAN_BAUD             = 33333

# Return codes
STATUS_NOERROR = 0
ERR_BUFFER_EMPTY = 0x10
ERR_TIMEOUT = 0x11


class PASSTHRU_MSG(Structure):
    _fields_ = [
        ("ProtocolID", c_ulong),
        ("RxStatus", c_ulong),
        ("TxFlags", c_ulong),
        ("Timestamp", c_ulong),
        ("DataSize", c_ulong),
        ("ExtraDataIndex", c_ulong),
        ("Data", ctypes.c_ubyte * 4128),
    ]


class SCONFIG(Structure):
    _fields_ = [("Parameter", c_ulong), ("Value", c_ulong)]


class SCONFIG_LIST(Structure):
    _fields_ = [("NumOfParams", c_ulong), ("ConfigPtr", POINTER(SCONFIG))]


class J2534Error(Exception):
    def __init__(self, code, msg=""):
        self.code = code
        super().__init__(f"J2534 error 0x{code:X}: {msg}")


def find_dll(name_contains: str = "OBDX") -> Optional[str]:
    """Locate a registered J2534 DLL via PassThruSupport.04.04."""
    import winreg
    subs = [r"SOFTWARE\PassThruSupport.04.04",
            r"SOFTWARE\WOW6432Node\PassThruSupport.04.04"]
    for sub in subs:
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub)
        except OSError:
            continue
        i = 0
        while True:
            try:
                name = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            try:
                dk = winreg.OpenKey(k, name)
                lib, _ = winreg.QueryValueEx(dk, "FunctionLibrary")
                if (name_contains.lower() in name.lower()
                        or name_contains.lower() in (lib or "").lower()):
                    return lib
            except OSError:
                continue
    return None


def _mk_msg(protocol, data: bytes, txflags=0) -> PASSTHRU_MSG:
    m = PASSTHRU_MSG()
    m.ProtocolID = protocol
    m.TxFlags = txflags
    m.DataSize = len(data)
    for i, b in enumerate(data):
        m.Data[i] = b
    return m


class J2534:
    def __init__(self, dll_path: Optional[str] = None):
        self.dll_path = dll_path or find_dll()
        if not self.dll_path:
            raise RuntimeError("No J2534 device registered (install the OBDX driver)")
        self.dll = ctypes.WinDLL(self.dll_path)
        u = c_ulong
        P = POINTER
        self.dll.PassThruOpen.argtypes = [c_void_p, P(u)]
        self.dll.PassThruClose.argtypes = [u]
        self.dll.PassThruConnect.argtypes = [u, u, u, u, P(u)]
        self.dll.PassThruDisconnect.argtypes = [u]
        self.dll.PassThruReadMsgs.argtypes = [u, P(PASSTHRU_MSG), P(u), u]
        self.dll.PassThruWriteMsgs.argtypes = [u, P(PASSTHRU_MSG), P(u), u]
        self.dll.PassThruStartMsgFilter.argtypes = [u, u, P(PASSTHRU_MSG), P(PASSTHRU_MSG), P(PASSTHRU_MSG), P(u)]
        self.dll.PassThruStopMsgFilter.argtypes = [u, u]
        self.dll.PassThruIoctl.argtypes = [u, u, c_void_p, c_void_p]
        self.dll.PassThruReadVersion.argtypes = [u, c_void_p, c_void_p, c_void_p]
        self.dll.PassThruGetLastError.argtypes = [c_void_p]
        self.dev: Optional[int] = None

    def _chk(self, rc):
        if rc != STATUS_NOERROR:
            buf = create_string_buffer(160)
            try:
                self.dll.PassThruGetLastError(buf)
            except Exception:
                pass
            raise J2534Error(rc, buf.value.decode(errors="ignore"))

    def open(self):
        dev = c_ulong()
        self._chk(self.dll.PassThruOpen(None, byref(dev)))
        self.dev = dev.value
        return self.dev

    def close(self):
        if self.dev is not None:
            try:
                self.dll.PassThruClose(self.dev)
            finally:
                self.dev = None

    def read_version(self):
        fw = create_string_buffer(96); dll = create_string_buffer(96); api = create_string_buffer(96)
        self._chk(self.dll.PassThruReadVersion(self.dev, fw, dll, api))
        return (fw.value.decode(errors="ignore"), dll.value.decode(errors="ignore"),
                api.value.decode(errors="ignore"))

    def read_vbatt(self):
        v = c_ulong()
        self._chk(self.dll.PassThruIoctl(self.dev, READ_VBATT, None, byref(v)))
        return v.value / 1000.0

    def connect(self, protocol, baud, flags=0):
        ch = c_ulong()
        self._chk(self.dll.PassThruConnect(self.dev, protocol, flags, baud, byref(ch)))
        return ch.value

    def disconnect(self, ch):
        self.dll.PassThruDisconnect(ch)

    def clear_filters(self, ch):
        self.dll.PassThruIoctl(ch, CLEAR_MSG_FILTERS, None, None)

    def set_config(self, ch, params: list[tuple[int, int]]):
        arr = (SCONFIG * len(params))()
        for i, (p, v) in enumerate(params):
            arr[i].Parameter = p; arr[i].Value = v
        lst = SCONFIG_LIST(len(params), arr)
        self._chk(self.dll.PassThruIoctl(ch, SET_CONFIG, byref(lst), None))

    def flow_control_filter(self, ch, tx_id, rx_id, proto=ISO15765):
        """Pair tx_id<->rx_id so the driver auto-handles ISO-TP flow control."""
        def idbytes(cid): return bytes([(cid >> 24) & 0xFF, (cid >> 16) & 0xFF,
                                        (cid >> 8) & 0xFF, cid & 0xFF])
        mask = _mk_msg(proto, b"\xFF\xFF\xFF\xFF")
        patt = _mk_msg(proto, idbytes(rx_id))
        flow = _mk_msg(proto, idbytes(tx_id))
        fid = c_ulong()
        self._chk(self.dll.PassThruStartMsgFilter(ch, FLOW_CONTROL_FILTER,
                  byref(mask), byref(patt), byref(flow), byref(fid)))
        return fid.value

    def pass_filter(self, ch, proto, pattern_id: int, mask_id: int):
        """Accept raw frames whose CAN id matches pattern_id under mask_id."""
        def idbytes(cid): return bytes([(cid >> 24) & 0xFF, (cid >> 16) & 0xFF,
                                        (cid >> 8) & 0xFF, cid & 0xFF])
        mask = _mk_msg(proto, idbytes(mask_id))
        patt = _mk_msg(proto, idbytes(pattern_id))
        fid = c_ulong()
        self._chk(self.dll.PassThruStartMsgFilter(ch, PASS_FILTER,
                  byref(mask), byref(patt), None, byref(fid)))
        return fid.value

    def write(self, ch, tx_id, payload: bytes, proto=ISO15765, txflags=ISO15765_FRAME_PAD, timeout=200):
        hdr = bytes([(tx_id >> 24) & 0xFF, (tx_id >> 16) & 0xFF, (tx_id >> 8) & 0xFF, tx_id & 0xFF])
        m = _mk_msg(proto, hdr + payload, txflags)
        n = c_ulong(1)
        self._chk(self.dll.PassThruWriteMsgs(ch, byref(m), byref(n), timeout))

    def read(self, ch, timeout=200, max_msgs=8):
        arr = (PASSTHRU_MSG * max_msgs)()
        n = c_ulong(max_msgs)
        rc = self.dll.PassThruReadMsgs(ch, arr, byref(n), timeout)
        if rc in (ERR_BUFFER_EMPTY, ERR_TIMEOUT):
            return []
        self._chk(rc)
        out = []
        for i in range(n.value):
            m = arr[i]
            data = bytes(bytearray(m.Data[:m.DataSize]))
            out.append(data)
        return out

    def query_iso15765(self, ch, tx_id, request: bytes, timeout=250):
        """Write an ISO-TP request and return the reassembled response payload
        (bytes after the 4-byte CAN id), or None. Skips flow-control frames."""
        self.write(ch, tx_id, request, timeout=timeout)
        import time
        end = time.monotonic() + timeout / 1000.0 + 0.1
        while time.monotonic() < end:
            for data in self.read(ch, timeout=timeout):
                if len(data) < 5:
                    continue
                payload = data[4:]
                # skip the echo of our own request / flow frames; want a reply
                if payload and payload[0] in (0x41, 0x49, 0x62, 0x7F, 0x59, 0x63):
                    return payload
        return None
