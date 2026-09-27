# Changelog

Versions before 0.18.0 are recorded in the git history (commit subjects carry
the version, e.g. `feat(delta): ... (v0.17.0)`).

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
