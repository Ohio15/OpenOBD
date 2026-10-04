# Changelog

Versions before 0.18.0 are recorded in the git history (commit subjects carry
the version, e.g. `feat(delta): ... (v0.17.0)`).

## 0.19.3 — 2026-10-04

### Added
- **Multi-file decode.** `parse_obdx_dir()` (and `j2534_decode.py <directory>`)
  decode every `OBDXGT_Log*.txt` in a folder in natural order, because the GT
  writes one file per J2534 session, so a full job (diagnostics + programming)
  spans several.
- **ECM identification capture.** `0x1A` read-by-local-id responses are captured
  separately (`DecodeResult.identification` / `identification_rows()`), with
  `gm_partnum()` decoding a 4-byte Cx value to a GM part number. The CLI prints
  the ECM's calibration part-number set — a record of what's flashed.

### Fixed
- **No phantom services.** `_is_request_sid` filters the GT's low-level handshake
  frames (seen on CAN id `0x101` as payloads `0xFD`/`0xFE`/`0xAE`), which were
  surfacing as fake `unknown_0x..` services. The service summary of a real
  programming log is now clean. Suite 292 passed.

## 0.19.2 — 2026-10-04

### Fixed — validated against a real OBDX capture
- **OBDX log format corrected.** The guessed line format in 0.19.1 was wrong.
  A real capture (OBDXGT fw 1.0.2.0) logs one frame per line as
  `… : PassThruWriteMsgs - Frame to Write: <hex>` (tx) and
  `… : PassThruReadMsgs - Frame Found: <spaced hex>` (rx), timestamp
  `HH:MM:SS:mmm`. `parse_obdx_log` now matches that exactly.
- **Complete-message model.** The J2534 DLL reassembles ISO-TP internally, so a
  capture carries whole UDS messages with **no PCI byte** (a VIN request logs as
  `0902`, not `020902`). `decode()` now treats each frame as a complete message
  by default (`frames_as_messages`); `decode(..., raw_can=True)` keeps the
  ISO-TP reassembly path for a protocol-CAN capture. This was re-running
  reassembly on already-assembled messages and producing phantom errors.
- **29-bit addressing.** `_resp_id_for` now pairs GM 29-bit UDS
  (`0x18DA<ecu>F1` → `0x18DAF1<ecu>`) as well as 11-bit, since the factory tool
  uses both.
- Validated on the real log: 7 frames, 0 reassembly errors, VIN read (mode 09)
  correctly identified, unanswered mode-22 F802 probe flagged. Suite 287 passed.

## 0.19.1 — 2026-10-04

### Added
- **OBDX native-log capture path (`parse_obdx_log`).** The OBDX Pro J2534 driver
  can log its own pass-thru traffic — `LoggingEnabled:1` in
  `%APPDATA%\OBDX Pro\J2534\Settings\OBDXGT_Config.cfg`, files land in the
  sibling `Logs\` dir — so a factory session can be captured with no proxy DLL
  to compile. `parse_obdx_log` reads that log; `j2534_decode.py --format obdx`
  (and `auto`) accept it. The decoder downstream is format-independent.
- **Inferred, pending validation.** OBDX does not document the log line format,
  so the parser matches the standard J2534 debug-log convention (direction from
  the `PassThru*Msgs` call, bytes after `Data:`, first 4 bytes = CAN id) and is
  deliberately tolerant. It MUST be validated against the first real OBDX
  capture; if the labels differ, only `_OBDX_*` in `j2534log.py` changes.

## 0.19.0 — 2026-10-04

### Added
- **Offline J2534 trace decoder (`openobd/j2534log.py`, `tools/j2534_decode.py`).**
  Turns a captured GM factory-tool pass-thru session (Techline Connect / GDS2 /
  SPS over the OBDX Pro GT's J2534 interface) into a candidate mode-22 DID table
  — the one open item on the live gauges, since `gt.DID_TABLE` is empty until
  each DID is correlated. Reassembles ISO-TP over raw CAN frames (same error
  model as `gt.reassemble_isotp`: short single frame, first-frame-before-complete,
  sequence gap and truncation are errors, never silent), pairs each `0x22`
  request with its `0x62` response on the GM 11-bit reply id, and emits observed
  read DIDs plus a per-service summary. Accepts JSONL / CSV / loose-hex traces.
  `--tsv` writes the DIDs in `ecm_dids.tsv` shape so `analyze_dids.py` picks them
  up for correlation. Tests in `tests/test_j2534log.py`, incl. a CLI end-to-end
  case.
- **J2534 logging proxy (`tools/j2534_trace_proxy/`).** A pass-through J2534
  v04.04 DLL the factory tool loads instead of the real GT DLL; it forwards every
  call and logs each frame in the decoder's JSONL format. C source + mingw / MSVC
  build scripts + an install README. paxson has no C compiler, so the DLL is
  built on NEXUS (or any box with mingw) and copied back; this is the one step
  not done from this seat.

### Scope (DR-011)
- Both pieces are **readers**. The decoder classifies every service on the bus
  but synthesises reusable output only for the read services (`0x22`, `0x01`,
  `0x09`, DTC modes). Programming / security services (`0x27` SecurityAccess,
  `0x34`/`0x36`/`0x37` transfer, `0x2E` write, `0x31` routine, `0x10`/`0x11`) are
  observed and counted, with the identifiers they touched reported, but no
  seed/key, transfer payload, or ordered sequence is ever emitted. Reconstructing
  the flash/unlock path is deliberately out of scope. Ron runs the capture.

## 0.18.4 — 2026-10-01

### Fixed
- **The Dashboard connected blind to a GT stuck in binary mode.** After any
  pass-thru session (an e38flash read, HP Tuners, a sniff) the GT answers text
  commands with binary frames. The Module Map already caught that, but the
  Dashboard's `ObdxGt.open()` configured the GT without checking, used the
  binary `AT@1` reply as the device name, and the Dashboard sat empty with no
  explanation. `open()` now identifies the GT first (`ATI`, then `AT@1`) and
  raises `GtBinaryMode` with the fix that works: unplug the GT's USB cable
  and the OBD plug for 10 s. A text reply that is not an ELM327/OBDX is
  refused too, and a failed open always releases the serial port.
- **A GT that stopped answering left the gauges frozen, looking live.** The
  poll loop swallowed every exception. `GtDataSource` now counts consecutive
  failures. After two, every channel reports `failed` (the "read failed"
  badge), `latest()` stops serving the last sample, and `status_message()`
  puts the reason in the status line. It returns to live on the next good poll.
- **ATRV on the poll path was parsed loosely**, with a regex that would pull
  a number out of noise. It now uses the strict `parse_atrv` and ignores
  binary replies, as the Module Map does.

### Changed
- README: the GT live path is documented, the "GtDataSource stub" line and
  "will implement" wording are gone, and the architecture tree lists all 19
  modules (`j2534.py` is noted as unused by the app).
- `gt.py`: the comment that said `DID_TABLE` was "left EMPTY" was wrong (it
  holds the verified TFT DID); `CANONICAL_KEYS` is defined once.
- `tools/*.py`: no absolute paths. Repo paths derive from the script's
  location, the HPT harvest folder is `~/hpt_extract` (override with
  `OPENOBD_HPT_EXTRACT`), and the helper interpreter is `sys.executable`.

### Tests
10 new (`tests/test_gt_dashboard.py`), plus a GUI smoke section driving the
real Dashboard through a failing and a recovered GT source. Five mutations of
the fix and one of the status-line wiring are each caught.

## 0.18.3 — 2026-09-30

### Fixed
- **GT autodetect could pick the wrong adapter** (from main, v0.15.3, PR #2).
  `ObdxGt.autodetect()` fell back to any USB-serial port, then to the first
  port at all, so with the GT absent the ELM stream could reach the OBDLink
  MX+. It now matches USB 0483:5740 only, exactly one device, and `open()`
  raises naming every port seen. The DID tools no longer hardcode COM3.

## 0.18.2 — 2026-09-26

### Fixed
- **DTC parser re-scanned inside consumed bytes.** `parse_dtc_response`
  searched the flattened reply for every `43`, so a 0x43 inside DTC data
  could start a phantom code. Replaced by `parse_dtc_reply`, which walks each
  responder's message by its declared structure (mode byte 43/47/4A, count
  byte, exactly count 2-byte DTCs) and never re-scans. 00 00 pairs and CAN
  padding beyond the declared length are not DTCs.
- **Multi-frame replies (more than 2 DTCs on CAN) were misread.** DTC reads
  now run with ATH1 + ATS0 + ATCAF1 (all confirmed; CAF1 was only the ATZ
  default before) and `reassemble_isotp` rebuilds each responder's ISO-TP
  message from its first/consecutive frames, including interleaved frames
  from several modules. A missing or out-of-order frame, a truncated message,
  or a count/length mismatch marks THAT module "Incomplete" in the codes
  table — never a silently short list. Regression fixture: the truck's own
  reply `7E81008430303050606` / `7E8210700` = P0305, P0606, P0700.

## 0.18.1 — 2026-09-26

### Fixed
- **DTC read / clear / readiness went out on whatever header was set** (in
  practice the 7E0 left by `open()`, so only the ECM was read or cleared).
  Modes 03/07/0A and the 04 clear now set the functional 7DF header — the
  parser is multi-ECU and the UI promises to clear ALL codes; J1979 defines
  these as functional. Readiness (0101) goes physical to the ECM (7E0): with
  headers off a functional reply interleaves ECUs. Every header and format
  command is checked and retried once.
- **Clear refuses to send 04** unless the header, header display and
  formatting are confirmed, and tells the user the clear was not sent.
- **Clear success needs a positive 44 frame from the ECM (7E8)**, parsed per
  frame; a 44 from another module or a negative response (7F 04 NRC) is not
  success, and the dialog says who acknowledged.
- **Unknown is never "no codes".** A DTC kind that could not be addressed or
  got no positive answer is shown as "Not examined"; readiness that could not
  be read shows "MIL: not examined".

## 0.18.0 — 2026-09-26

Module Map correctness, from running 0.17.0 against the truck (2010 Silverado
1500, E38 / T43, OBDX Pro GT).

### Fixed
- **Module addresses.** TCM is 7E2/7EA (was 7E1/7E9); EBCM is 243/643 (was
  241/641, which is the BCM). The BCM moves to the HS-GMLAN rail at 241/641 and
  is pinged like the other HS modules. Evidence: 2026-08-22 HS-CAN sniff
  (responses on 7EA, 7EB, 7EC, 641, 643, 64D), truck-mcp's module table, and
  HP Tuners reaching BCM/EBCM at 0x541/0x543 UUDT. 7EB/7EC/64D answer too but
  are unidentified and deliberately not named.
- **Functional broadcast was not a broadcast.** `scan_network` sent `0100` on
  the 7E0 header left by `open()`, i.e. a physical ECM request, so only 7E8
  could answer. The 7DF header is now set (and checked) first.
- **`interface_alive` accepted any bytes.** It now requires an ELM/OBDX text
  identity (ATI names ELM or AT@1 names OBDX). A GT left in its binary J2534
  mode (seen answering `7F 02 41 01 3C`) gets its own verdict: unplug it from
  USB and the OBD port for 10 s, then rescan. It is no longer blamed on DLC
  power.
- **Garbled ATRV reported as "no DLC voltage".** Only a parsed low reading
  fails the DLC stage; an unparseable reply is "voltage unreadable" and the
  scan continues.
- **ATSH/ATCRA replies unchecked.** Every addressing command is checked for OK
  and retried once (the GT intermittently answers `?`). A module whose
  header/filter cannot be confirmed is the new grey **not examined** state,
  never red. Header restores are checked; if one fails, ECM polling and DID
  reads re-establish 7E0 before trusting a reply, or return nothing.
- **Broadcast absence treated as silence.** A module is red only after a
  verified physical ping got no answer. Pings match reply frame ids, not
  substrings of data bytes.

### Added
- `Status.NOT_EXAMINED`, `vehnet.scan_pipeline()` (the scan flow, testable
  without Qt), a legend entry, and a legend that wraps on narrow views.
- Windows version resource on the exe (from `__version__`) and the version in
  the About dialog.
