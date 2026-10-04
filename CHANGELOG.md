# Changelog

Versions before 0.18.0 are recorded in the git history (commit subjects carry
the version, e.g. `feat(delta): ... (v0.17.0)`).

## 0.22.5 — 2026-10-04

### Added
- Functional `$A9` sweep (`ObdxGt.sweep_gmlan_dtcs`): one read-only DTC request
  to every HS-GMLAN node (AllNodes 0x101, FE-framed as e38flash verified),
  then every id that answered is listed. Responders outside the identified set
  are flagged UNACCOUNTED with their fault codes; `$A9` refusals are listed as
  "a module is there". This locates modules by their answer instead of by
  guessing addresses (for the truck's unlocated transfer case module). The
  dtcscan CLI prints it after the per-module lines.

## 0.22.4 — 2026-10-04

### Added
- The DTC scan also reads the two HS-GMLAN ids that answer on this truck but
  are not identified: 0x242 (answered `$A9` with an 18-entry table in August,
  silent to every ident DID) and 0x24D (NAKs `$A9`). They print by address
  only and are never added to the module map. The truck has an electric-shift
  transfer case (RPO NQH) whose control module is not yet located; transfer
  case codes (C03xx) in 0x242's table would identify it. Wanted for the
  recurring Service 4WD message.

## 0.22.3 — 2026-10-04

### Fixed
- **The EBCM/BCM scan listed the whole supported-DTC table as faults.** GMLAN
  `$A9 81` with mask FF returns every DTC the calibration supports, each with a
  status byte. `parse_a9_report` ignored that byte, so the first truck scan
  printed 35 ABS and 47 body "codes". It now applies truck-mcp's verified rule
  (`gmlan.fault_codes`): a code is a fault only when status bit1 (currently
  failed) is set or the status is not a housekeeping value (01/19/21/25).
  Unknown statuses count as faults. The full table moves to `codes["table"]`.
- The 0.22.2 "C0035 verified" claim was wrong. The real capture shows C0035 at
  status 01, a healthy table entry; the live fault read D3 in August. The test
  that asserted otherwise is rewritten.
- Same class in `parse_uds19_reply`: records whose only status bits are
  "test not completed" (ISO 14229 bits 4 and 6) are no longer reported as codes.
- `dtcscan` prints each fault with its status byte and counts the healthy
  table entries instead of listing them.

## 0.22.2 — 2026-10-04

### Fixed — $A9 report frame format (VERIFIED on the truck, C0035 decoded)
- The GT returns the `$A9` report frames WITHOUT a CAN id prefix (ATCRA + headers
  off): `81 <hi> <lo> <symptom> <status>` padded to 8 bytes, e.g.
  `8140355A01000000` = C0035. `parse_a9_report` now decodes that form (keeping
  the id-prefixed form too), dedups a DTC that repeats with different symptom
  bytes, and handles the no-id negative `7F A9 <nrc>`. A verbatim slice of the
  real EBCM capture is a test fixture; C0035 (the known left-front fault) decodes
  — ground truth for the whole GMLAN read path. Suite 328.

## 0.22.1 — 2026-10-04

### Fixed — GMLAN $A9 uses ELM, not STN (the GT is not an STN device)
- The truck showed the OBDX Pro GT rejects `STP` — it is not an STN chip, so
  0.22.0's STN sequence (STP/STFAP) could never run. `read_gmlan_dtcs` now uses
  plain ELM327: force CAN 11/500 (`ATSP6`), CAF off, set the tx header, widen the
  receive filter to the module's report id with `ATCRA`, send `03 A9 81 FF`,
  then restore automatic protocol + CAF. The full GT response is always kept in
  `raw` (and surfaced in the error when nothing decodes) so the first truck run
  reveals the exact frame shape — bring-up over inference, since no tool has run
  `$A9` over the GT's ELM firmware before.

## 0.22.0 — 2026-10-04

### Added — GMLAN $A9 DTC read for the EBCM/BCM
- The chassis/body modules NAK UDS `$19` (confirmed on the truck). They answer
  GM service **`$A9 81`** instead. **`gt.read_gmlan_dtcs(req_id)`** reads them:
  select raw HS-CAN (`STP 31`), CAF off, narrow receive filters for the module's
  UUDT report id (req+0x300, EBCM 0x543) and USDT negative id (req+0x400), send
  `03 A9 81 FF`, capture the report frames, then RESTORE automatic protocol
  search and CAF. The sequence is ported from truck-mcp's `read_chassis_dtcs`,
  which reads this truck's C0035 live.
- **`gt.parse_a9_report`** decodes the per-frame UUDT reports (2-byte GMLAN DTC
  via `format_dtc`, symptom + status), filtered to the module's id; the 00 00
  marker is a clean "no codes", silence stays "not examined".
- **`command(..., deadline=)`** adds a capture window for the multi-frame report.
- `vehnet.scan_all_module_dtcs` now routes the powertrain modules (ECM, TCM) to
  the OBD modes and the GMLAN modules (EBCM, BCM) to `$A9`. 22 module-DTC tests;
  full suite 327. `_run_dtcs.bat` launcher added.

### To verify on the truck
- Re-run `_run_dtcs.bat`: the EBCM should now list **C0035** (your known
  left-front wheel-speed fault). That is the ground-truth confirmation of the
  `$A9` path. If STP is rejected or the EBCM stays silent, the error says so.

## 0.21.0 — 2026-10-04

### Added — per-module DTC read (reaches the EBCM/BCM, not just the OBD channel)
- **`gt.read_module_dtcs(req_id, resp_id, uds=)`** reads DTCs from ONE module by
  physical addressing: the ISO15765 powertrain modules (ECM, TCM) via OBD modes
  03/07/0A, the GMLAN chassis/body modules (EBCM, BCM) via UDS `$19 02`, which the
  functional `read_dtcs()` cannot reach because they don't answer the OBD
  broadcast. Header restored to the ECM afterward; silence is "not examined",
  never "no codes".
- **`gt.parse_uds19_reply`** decodes a `$19 02` reply (3-byte DTC + status),
  per-responder, reusing the ISO-TP reassembler and `format_dtc`.
- **`vehnet.scan_all_module_dtcs(gt)`** reads every reachable module in one pass;
  SW-GMLAN modules (IPC, SDM, HVAC, radio, TCCM) have no HS id and are reported
  `unreachable`, not dropped.
- **`openobd/dtcscan.py`** — headless `python -m openobd.dtcscan`: connect the GT
  and print per-module codes as text. 13 tests (UDS parse incl. C0035, the OBD
  and UDS read paths, NAK→legacy hint, silence handling, orchestration, CLI
  formatting). Full suite 318.

### To verify on the truck
- The EBCM path uses UDS `$19` by default; the known **C0035** is the ground
  truth. If the EBCM line shows C0035 the path is correct; if it NAKs `$19`, the
  module uses a GM legacy DTC service and we switch the EBCM/BCM path to that.

## 0.20.1 — 2026-10-04

### Added — UAC-style approval popup (closed-loop step A, increment 2)
- **`tools/approve-dialog.ps1`.** A topmost desktop popup (like UAC) that shows a
  pending elevated-risk action and offers Allow / Reject, for Ron's "popup I can
  click to allow or reject" flow. Gates on exit code (0 allow; 2/3/4/other deny);
  fail-safe — traps every error and exits deny, never allow. `-SelfTest`
  validates args + the decision-record write with no UI.
- **`docs/APPROVAL-POPUP.md`.** The guard-integration contract: the popup is
  raised by the trusted hazard-guard (like UAC is raised by the OS), run
  synchronously, allow only on exit 0. Writes are never routed through it. The
  DR-011 edit that wires the guard to call it is the operator's; this is the
  contract it implements.

## 0.20.0 — 2026-10-04

### Added — phone approve/deny gate (closed-loop step A, increment 1)
- **`openobd/approval.py`.** The mechanism that lets the agent run ECM READS
  behind a phone approve/deny instead of a terminal hand-off (Ron's 2026-10-03
  decision). The agent is the gated party, so the design makes it unable to
  approve its own action: the agent only requests and verifies; a decision is
  signed with a key the agent does not hold and read from a store it cannot
  write; each approval is bound to one random request id, fresh (TTL), and
  single-use. Writes are never gate-eligible — they stay hard-gated.
- `ApprovalGate.request()` sends the prompt via a `Notifier` seam; `check()` /
  `await_decision()` return a verified, fresh, unconsumed approval or raise on
  deny / expiry / forgery / replay. `sign_decision` / `verify_decision` are the
  one shared signature definition for the trusted endpoint and the agent.
  `RecordingNotifier` and `JsonlDecisionSource` are the dev/next-increment seams.
  13 tests, incl. forged-key, tampered-field, wrong-request, replay, expiry.
- Does NOT touch the hazard-guard. Wiring DR-011 to consult this gate is the
  operator's change. Next: the ntfy-button transport + the trusted callback
  endpoint that signs decisions + the key placement, then the OpenOBD read tool.
  Suite 305 passed.

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
