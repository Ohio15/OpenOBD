# J2534 trace proxy

A logging pass-through J2534 (v04.04) DLL. A GM factory tool (Techline Connect /
GDS2 / SPS) loads it **instead of** the real OBDX Pro GT J2534 DLL. It forwards
every call to the real DLL unchanged and appends each CAN frame to a trace file.
The factory tool and the truck behave exactly as they would without it; this is
a read-only tap on the pass-thru API.

The trace is decoded offline by `../j2534_decode.py` into a candidate mode-22
DID table, which is what OpenOBD needs to fill `gt.DID_TABLE` (empty on purpose
until each DID is correlated).

## Scope / fence (DR-011)

This proxy only **forwards and logs**. It originates no traffic, and the offline
decoder deliberately does **not** turn captured programming/security bytes into
a runnable flash or unlock sequence — it reports that such traffic occurred and
which identifiers it touched, nothing more. Ron registers and runs this; the
capture is his action. The agent never speaks to the J2534 link.

## Build

paxson has no C compiler, so build on a box that does and copy the DLL back:

- **mingw (NEXUS / any Linux):** `apt-get install gcc-mingw-w64` then `./build.sh`.
  Produces `j2534_proxy_x86.dll` (and `_x64` if the 64-bit cross-gcc is present).
- **MSVC (Windows):** open the *x86* (or *x64*) *Native Tools Command Prompt for
  VS* and run `build.bat`.

Match the DLL bitness to the factory tool's process (Techline Connect is
usually 32-bit → build x86).

## Install (Ron, once, elevated)

1. Find the real OBDX Pro GT J2534 DLL. Its path is the `FunctionLibrary` value
   under one of:
   `HKLM\SOFTWARE\WOW6432Node\PassThruSupport.04.04\<OBDX...>` (32-bit) or
   `HKLM\SOFTWARE\PassThruSupport.04.04\<OBDX...>` (64-bit).

2. Put `j2534_proxy_x86.dll` somewhere stable, e.g. `C:\obdx-proxy\`, with a
   `j2534_proxy.ini` beside it:
   ```
   real_dll=C:\Program Files (x86)\OBDX\OBDXProGT_J2534.dll
   trace_log=C:\obdx-proxy\trace.jsonl
   ```
   (Or set env vars `OBDX_REAL_J2534` / `OBDX_TRACE_LOG` instead of the ini.)

3. Register the proxy as its own PassThru device so the factory tool lists it.
   Create a key under `HKLM\SOFTWARE\WOW6432Node\PassThruSupport.04.04\` named
   e.g. `OBDX Pro GT (trace)` with string values copied from the real device's
   key, but `FunctionLibrary` pointing at the proxy DLL:
   ```
   Name           = OBDX Pro GT (trace)
   FunctionLibrary= C:\obdx-proxy\j2534_proxy_x86.dll
   Vendor         = (copy from real)
   ProtocolsSupported / CAN / ISO15765 / ... = (copy from real)
   ```
   `install_proxy.reg.example` shows the shape — edit the paths, do not import it
   blind.

4. In Techline Connect / GDS2, pick **OBDX Pro GT (trace)** as the J2534 device.

## Capture and decode

1. Connect the GT to the truck and USB (replug it out of binary pass-thru mode
   first). Battery maintainer on.
2. Run the factory session (e.g. an SPS vehicle read, or a GDS2 DTC/data read).
3. The trace lands at the `trace_log` path. Decode it:
   ```
   python tools/j2534_decode.py C:\obdx-proxy\trace.jsonl --tsv candidate_dids.tsv
   ```
4. Correlate the candidate DIDs against known live values
   (`tools/analyze_dids.py`) before adding any to `openobd/gt.py` `DID_TABLE`.

## Uninstall

Delete the `OBDX Pro GT (trace)` registry key; the real device key is untouched.
Nothing about the real DLL or the device is modified at any point.
