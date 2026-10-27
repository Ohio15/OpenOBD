# ADR-0001: Active Tests actuation (v0.40.0), Architecture A

| Field | Value |
|---|---|
| Status | **Proposed.** Documentation only: nothing in this ADR is wired, and nothing here has touched a vehicle |
| Date | 2026-10-27 |
| Decider | Ron |
| Decision records | D1 control list `d7fedb9f`, **D2 architecture `0f5688ed`**, D3 interlocks `e2c962dd`, D4 truck-mcp landing `b2941096`. Survey: `e4b936a6` |
| Repositories | `Ohio15/OpenOBD` (this repo, **public**) and `Ohio15/truck-mcp` (**private**) |
| Supersedes at flag flip (not before) | README "Active Tests" read-only paragraph, manual test AT-02 (split, see section 9) |

This repository had no ADR convention before this file. `docs/adr/ADR-NNNN-<slug>.md`
starts here.

---

## 1. Context

### 1.1 What exists today (verified in code, 2026-10-27)

**truck-mcp** (branch `feat/control-foundation`, HEAD `e7d0c7e`, PR #5 open, 12
ahead / 0 behind `origin/main`; `main` has no `session.py`):

- `src/truck_mcp/control.py` is pure: it holds encoders, NRC classes, the
  frame guards and `ControlCandidate`. Its executability invariant is enforced
  on `__new__` and `_make`. `CONTROL_REGISTRY = ()` is empty by design.
- `src/truck_mcp/session.py` has:
  - `ControlLease`: context manager, `arm` / `fire` / `activate` / `release`,
    and the fixed abort order in `_abort`.
  - `ControlJournal`: fsynced JSONL. Records are written before the frame, and
    a failed journal write refuses the frame.
  - `evaluate_preconditions`: a pure gate that fails closed.
  - `RawObdSampler`, `recover_indeterminate`, and the per-process
    `_module_lock`.
  - `HEARTBEAT_PERIOD_S = 1.0`, `HEARTBEAT_DEADLINE_S = 1.5`, `MAX_LEASE_S = 120`
    (`config.CONTROL_MAX_LEASE_S`), `ARM_TICKET_TTL_S = 60`.
- **The lease is bound to the ELM/STN text protocol at five points:**
  1. `ControlChannel` accepts only `AT`/`ST` adapter commands and tracks `ATSH`.
  2. `open_control_session` / `close_control_session` issue
     `STP`/`ATCAF0`/`ATST`/`STFAP`.
  3. `gmlan.isotp_request` / `_transmit` / `_settle` build PCI frames
     themselves (ATCAF0) and parse adapter text lines.
  4. `RawObdSampler` reads `ATRV`.
  5. Loss detection compares the length of `transport.recoveries` (ATZ
     self-heal).
- `elm.ControlTransport` already exists, but only as a **replay-refusing view
  over `ElmTransport`**. It is not an adapter abstraction. The name is reused
  below on purpose, so section 4.1 renames the old class.
- The gate today checks:
  - speed = 0 from PID `0D`, plus a channel-liveness proof;
  - battery 12.0 to 15.5 V (ATRV, PID 42 corroborating);
  - a "park" attestation only when the engine is running;
  - **no RPM window**;
  - `FORBIDDEN_SPEED_SOURCES` refuses EBCM / wheel-speed data as a speed source
    (C0035: LF sensor dead, ABS and StabiliTrak disabled).
- `wheelspeed.py` states that the frame, byte and scale mapping for 0C1/0C5 is a
  hypothesis. The EBCM refuses `$AA`. **No PRNDL decoder exists in either
  repository.**
- The control core imports only the stdlib, `pyserial` and sibling modules
  (`control`, `session`, `elm`, `gmlan`, `obd`, `config`, `paths`, `enhanced`,
  `monitors`). `mcp` and `cantools` are server-only.
- `paths.data_root()` resolves **beside `sys.executable` when frozen**. Inside
  `openobd.exe` that means the OpenOBD install directory.

**OpenOBD** (`main` = `b0a1390`, v0.39.1):

- `openobd/j2534.py` is a ctypes J2534 v04.04 client for the OBDX Pro GT. It
  supports raw `CAN` and `ISO15765` channels, pass and flow-control filters,
  and `READ_VBATT`.
- `openobd/devctl.py` is a Ron-run CLI that has never been run (there are zero
  `devctl-*.jsonl` files). It:
  - uses raw `CAN` on J2534 with pass filters and 1 s `$3E`;
  - checks interlocks rpm >= 400, speed 0, **Neutral attested**;
  - sends `$20` on every exit;
  - streams TCCM position with `$2C` + periodic `$AA 03`. truck-mcp refuses both
    (`FORBIDDEN_SERVICES["2C"]`, `DPID_PERIODIC_REFUSED`).
  - Its source notes that the OBDX driver "can kill the process at close", so
    it opens the device once per process.
- `openobd/data/controls.json` holds 27 Autel-captured `$AE` entries, all with
  `bus: hs`: 7E4 TCCM, 7E2 TCM and 241 BCM. It has no ECM entries and no
  name, hazard or interlock fields. **The file is not in `openobd.spec`
  `datas`.**
- `openobd/activetests.py` is a read-only journal viewer over `ctljournal.py`.
  `tests/test_activetests_ui.py:309-385` AST-audits **that one file**:
  `_RO_ALLOWED_IMPORTS`, and `_RO_BANNED_CALLS` including
  `ControlLease/arm/fire/activate/release`.
- OpenOBD has no feature-flag mechanism. Persistent settings go through one
  chokepoint, `appsettings.read_settings` / `write_settings`.
- The default log folder is `Documents\OpenOBD\Logs` (OneDrive-redirected on
  this workstation). The fallback is `%LOCALAPPDATA%\OpenOBD\Logs`, and the
  user can change it (`logs/dir`).
- `.github/workflows/security-audit.yml` runs
  `pip-audit --strict --no-deps --disable-pip` on every `requirements*.txt`.
  CI runs on GitHub-hosted runners against PR-controlled code with
  `persist-credentials: false`.

### 1.2 GMW3110 (February 2010) facts the design rests on

The research agent checked these against the specification text:

1. **A device control ends on TesterPresent timeout.** Section 8.20 / data
   dictionary: `Request_DeviceControl_exit` "is used ... to cancel active device
   control functions if a TesterPresent ($3E) timeout occurs or if a
   ReturnToNormalMode ($20) request is received". P3C nominal is **5000 ms** and
   maximum **5100 ms** (Table 33). "The tester shall always use a P3C value less
   than the specified P3Cnom value", and the spec recommends a TesterPresent
   every 2 s. **The heartbeat is the fail-safe.** Losing it ends the control
   inside the module with nothing sent.
2. **CPID `$00` cancels every active device control on the module.** Releasing
   one control means sending the same CPID with its enable bits cleared.
3. **DTC setting is inhibited while `$AE` is active** unless `$10 03`
   (enableDTCsDuringDevCntrl) is sent first. Inhibition persists "until after a
   TesterPresent timeout occurs or a $20 service is requested" (section 8.2.2.1).
4. **Safety and theft CPIDs need `$27`**, otherwise NRC `$31`. On `$AE`, an NRC
   `$31` therefore does not prove the CPID is unsupported.
5. **NRC `$E3` DeviceControlLimitsExceeded** (section 7.2.14) means "all active
   device control commands have been terminated". It carries a 2-byte GM reason
   code from a non-public list, and "can be sent any time a limit has been
   exceeded, not just in response to a request message". **Recovery must not
   need a key cycle.**
6. **No public source names the BCM/TCM/TCCM CPID bits** (brain `824fafa5`).
   Names can only come from a listen-only capture taken while the Autel drives
   each menu item.

### 1.3 What the Autel capture shows

Source: `canrec-main-20261005-154648.jsonl`, 4584 frames, analysed read-only on
2026-10-27.

- **Keep-alive form:** the Autel sent the bare GMLAN `3E` (SF `01 3E`) to
  **7E4, 7E2 and 7E0**, and padded `01 3E 00..` to 241. It never sent the UDS
  form `3E 00`. truck-mcp's `control.dialect_for()` defaults 7E0-7E7 to UDS, so
  **registry entries must carry `dialect = gmlan` explicitly.**
- **Keep-alive cadence:** median gap 0.60 s on 7E4, 0.55 s on 7E2 and 0.60 s
  on 241.
- **No `$10 03` was sent.** The Autel ran every test with DTC setting
  inhibited, which is the GMW3110 default. No functional `$3E` on `101`.
  **No `$20`.**
- **"Commanded OFF" is not a release.** The capture shows two shapes:
  - `AE13 01 01` (mask 01, value 01) then `AE13 01 00` (mask 01, value 00). The
    control is still held at value 0.
  - The real releases are the mask-cleared forms `AE01 00 00 00 00 00`,
    `AE30 00 00 00 00 00` and `AE38 00 00 00 00 00`, or `AE00` (BCM). All of
    them were captured with positive responses.
- **TCCM motor drive:** `AE01 00 00 01 B2 00` then `AE01 00 00 01 32 00` (the
  0x80 enable bit cleared, per fact 2), then `AE01 00..00`.
- The Autel used `$2C` / `$AA` for feedback on all three modules.

---

## 2. Decision: Architecture A

OpenOBD consumes truck-mcp's control core in-process, as a pinned dependency
(never vendored, never copied). The core comprises:

- `ControlLease`
- `arm` / `fire` / `release` / `_abort`
- `ControlJournal`
- `evaluate_preconditions`
- `recover_indeterminate`

`ControlLease` gains a transport abstraction with two implementations:

- **`StnRawTransport`:** today's behaviour, MX+ over STN. Lives in truck-mcp.
- **`J2534Transport`:** OBDX Pro GT over SAE J2534. Lives in OpenOBD.

The GUI supplies `challenge_sink` as a two-step Arm then Fire dialog with a
lease countdown, Abort and Release-all. At startup the registry is built from
`controls.json` entries that meet the D1 admission rules and pass schema
validation. Everything ships inert behind a flag. The flag flip waits for
security-gate x2, `/security-audit`, and a first bench firing with Ron present.

## 3. Rejected alternatives (from D2)

| Option | Why rejected |
|---|---|
| **B:** promote `devctl.py` into the GUI | It is a second implementation of the interlocks, lease and abort. Two divergent safety implementations is debt by construction. `devctl.py` already disagrees with D3: it attests Neutral, has no Park, no voltage gate, no journal-before-frame, and uses `$2C` / periodic `$AA`. |
| **C:** truck-mcp as a loopback HTTP actuation service over the MX+ | It adds a network actuation surface that OEM and aftermarket tools never expose. It reverses truck-mcp's own "HTTP cannot reach ControlLease" tests (`tests/test_app_diag.py`), and it puts two adapters and two processes on the bus. |

**Also rejected inside A:** a J2534 ISO15765 (driver-segmented) channel for
control traffic. The reasons:

- It is a second ISO-TP implementation, in vendor firmware we cannot test.
- It hides FC and `7F xx 78` frames from the lease.
- It cannot see GMLAN UUDT or broadcast frames on the same channel.
- It builds the frames the guard needs to inspect.

Raw `CAN` on J2534 is already proven on this GT by `devctl.py`'s `Wire`, and it
reuses truck-mcp's reviewed ISO-TP code unchanged (section 4.2).

---

## 4. Interface contract

### 4.1 Naming and layering

- **New module `truck_mcp/ctltransport.py`** holds the `ControlTransport`
  protocol, the frame-level base `RawFrameControlTransport` and the value
  types.
- **`elm.ControlTransport` is renamed `elm.ElmNoReplayView`.** Its behaviour is
  unchanged, and it becomes an internal of `StnRawTransport`. One name must not
  mean two things on a safety path.
- `ControlLease.__init__(transport: ControlTransport, ...)` is the only
  constructor shape. The lease never sees `AT`/`ST` strings again.
  `ControlChannel` keeps every refusal it has today:
  - the lease-held check;
  - `FORBIDDEN_SERVICES` (`$28 $2C $A5 $34 $36 $37`);
  - even `$27`;
  - `$2F` / `$31` probe targets;
  - periodic `$AA`;
  - non-blocking exclusivity.

  It applies these to **payloads** before segmentation and again to **frames**
  in `_write_frame` (`frame_service` already reads first frames).
- **One ISO-TP implementation.** `gmlan._transmit` / `_settle` /
  `isotp_request` are refactored to operate on `Frame` objects instead of
  adapter text lines. Both transports use them. Multi-frame TX (FF + CF paced
  by the module's FC, BS/STmin, WAIT up to `MAX_WAIT_FRAMES = 10`) and
  `$78`-pending handling with `on_pending` heartbeats behave exactly as they do
  over STN today.

### 4.2 The protocol

```python
# truck_mcp/ctltransport.py  (contract; signatures are normative)
Bus = Literal["hs", "sw"]

class Frame(NamedTuple):
    can_id: str          # 3-hex 11-bit id, upper case
    data: bytes          # 1..8 bytes as on the wire (PCI included)
    at: float            # transport clock() at receipt / at write
    direction: Literal["rx", "tx"]

class TeardownReport(NamedTuple):
    steps: tuple[str, ...]
    errors: tuple[dict, ...]

class ControlTransport(Protocol):
    name: str                                   # "stn-raw" | "j2534"
    @property
    def is_open(self) -> bool: ...
    @property
    def epoch(self) -> int: ...                 # +1 whenever the link was re-established underneath
    def clock(self) -> float: ...               # monotonic seconds
    def sleep(self, seconds: float) -> None: ...

    # session -- raw CAN, filters admit exactly rx_ids (module USDT/UUDT, ECM, interlock sources)
    def open_session(self, bus: Bus, rx_ids: tuple[str, ...]) -> None: ...
    def set_target(self, tx_id: str, rx_id: str) -> None: ...      # "set tx/rx header"
    def close_session(self) -> TeardownReport: ...                  # never raises; always last

    # I/O -- single-shot, never retried, never scheduled
    def send_payload(self, payload: bytes, *, timeout_s: float) -> list[Frame]: ...
        # one ISO-TP request (SF, or FF+CF paced by the module's FC); returns tx frames.
        # Raises ControlIndeterminate(sent=True|False|None) if bytes may have left and the link failed.
    def receive(self, timeout_s: float, *, quiet_gap_s: float) -> list[Frame]: ...
        # every admitted frame, INCLUDING unsolicited ones (UUDT, unsolicited 7F AE E3)
    def tester_present(self, tx_id: str, payload: bytes, *, capture_s: float) -> list[Frame]: ...
        # ONE $3E and its capture. Caller-driven. Never a timer, never device-periodic.
    def battery_voltage(self) -> float | None: ...                 # ATRV / J2534 READ_VBATT
```

`RawFrameControlTransport` implements `send_payload`, `receive` and
`tester_present` on top of the shared ISO-TP code. Each implementation supplies
only four frame primitives: `_write_frame(can_id, data)`,
`_read_frames(timeout_s)`, `_set_filters(ids)` and `_open/_close`.

**Binding rules every implementation must meet.** truck-mcp exports a
conformance suite, `tests/transport_conformance.py`, as an importable pytest
module. OpenOBD runs it against `J2534Transport`.

1. **No device-scheduled heartbeat.** Both `PassThruStartPeriodicMsg` and an
   explicit timer satisfy GMW3110's P3C. The contract requires the explicit,
   caller-pumped beat. A device-periodic `$3E` keeps the module in device
   control while the host process is hung (frozen GUI, wedged worker, debugger
   break) as long as the USB link and driver stay up. That defeats truck-mcp's
   governing principle that heartbeat loss must itself be the safe state. A
   periodic message also yields no per-beat `$7E` proof that the module is
   still in session.
   - `J2534Transport` issues `CLEAR_PERIODIC` at open.
   - An AST test proves `PassThruStartPeriodicMsg` is never bound or called in
     `openobd/`.
2. **Single-shot I/O.** No method retries, replays or self-heals inside a
   session.
   - STN: `ElmNoReplayView` refuses `request_raw` / `command`. ATZ recovery
     increments `epoch`.
   - J2534: any `J2534Error` on write or read after bytes may have left raises
     `ControlIndeterminate`. A reconnect increments `epoch`.

   The lease treats any `epoch` change as `SessionLost`. This replaces the
   `len(recoveries)` comparison.
3. **No own-TX echo as RX.** J2534 `LOOPBACK = 0`. Any TX frame a driver
   reports is tagged `direction="tx"` and never parsed as a reply.
4. **Identical bytes on both adapters.** Frames are 8-byte DLC with
   `gmlan.encode_single_frame` padding on both transports, so the journal's
   `request` and `frame` bytes are adapter-independent.
5. **Unsolicited frames are delivered.** `receive` returns frames that answer
   no request. The J2534 driver queue buffers continuously. An STN adapter only
   reports frames during capture windows (about 35% duty at a 0.35 s capture
   per 1.0 s beat), so an unsolicited `E3` can be missed there. That error is
   in the safe direction (the UI shows a control as held when the module has
   already dropped it), and the next `$AE` reply or the release reveals it.
6. **One owner, one thread.** Exclusivity stays non-blocking (`ControlChannel._exclusive`).

### 4.3 Timing requirements

| Requirement | Value | Source / enforcement |
|---|---|---|
| Heartbeat schedule | 1.0 s, send-to-send (`HEARTBEAT_PERIOD_S`) | session.py |
| Heartbeat deadline | 1.5 s since last beat sent, otherwise `SessionLost` (`HEARTBEAT_DEADLINE_S`) | session.py `_guard` |
| Module timeout | P3Cnom 5000 / P3Cmax 5100 ms | GMW3110 Table 33. The design holds a 3.5 s margin and does not depend on which value these modules use |
| Heartbeat capture (P2 budget) | 0.35 s | `HEARTBEAT_CAPTURE_S`. J2534 `ReadMsgs` timeout <= this |
| Control / release capture | 0.5 s / 0.3 s | `CONTROL_CAPTURE_S` / `RELEASE_CAPTURE_S` |
| J2534 `WriteMsgs` timeout | <= 200 ms | J2534Transport. A slower write is a transport error |
| Lease cap | 120 s hard, from lease open (`MAX_LEASE_S`) | session.py `_guard`, `LeaseExpired` |
| Arm ticket TTL | 60 s, burn-on-any-failure, max 4 outstanding | session.py `arm` / `fire` |
| Precondition freshness | each reading <= 1.0 s old at the moment of judgement | `PRECONDITION_MAX_AGE_S` |
| ISO-TP TX | SF <= 7 B; FF+CF up to 4095 B, FC BS/STmin honoured, WAIT <= 10, FC wait 0.5 s | gmlan.py (shared). Every Phase 1 request is 7 bytes (SF), but the contract requires MF |
| Response pending `$78` | <= 10 pendings, <= 30 s total, heartbeat between captures (`on_pending`) | gmlan.py; lease `_pending_beat` |

**D2's revisit trigger is measurable.** If the GT cannot hold the 0.35 s
heartbeat capture and the 1.5 s deadline over raw CAN on Windows, D2 is
revisited. Stage 0 measures this at the bench with zero actuation (`$3E` +
`$20` only), as step 1 of section 8 PR O3.

### 4.4 Changes that land in **truck-mcp**

| # | Change |
|---|---|
| T-a | `ctltransport.py`: `ControlTransport`, `Frame`, `RawFrameControlTransport`, `StnRawTransport` (wraps `ElmTransport` through the renamed `ElmNoReplayView`). `open_control_session` / `close_control_session` move into `StnRawTransport`. |
| T-b | `gmlan` ISO-TP on `Frame` objects (single implementation). `RawObdSampler` reads voltage through `transport.battery_voltage()`. |
| T-c | **Interlock providers**, replacing the hard-wired `RawObdSampler` as the only source: a `SpeedSource` protocol and a `RangeSource` protocol, both injected through `ControlLease(..., speed_source=, range_source=)`. Details below this table. |
| T-d | **Per-control interlock profile** on `ControlCandidate`, as new trailing fields with defaults so existing construction is unchanged: `rpm_window: tuple[float, float] \| None`, `range_mode: "sensed" \| "attested"`, `drive_capable: bool = False`, `dialect: str`, `max_hold_s: float`. `_prepare_activation` (which runs at **both** arm and fire) enforces the candidate's RPM window from PID `0C`, its range mode, a mandatory parking-brake attestation, and `drive_capable` only for names in `DRIVE_CAPABLE_WHITELIST = ()`, which ships empty per D1. The lease-level `require_engine_off` stays as a floor. |
| T-e | **Registry injection.** `ControlLease(..., registry: tuple[ControlCandidate, ...] = ctl.CONTROL_REGISTRY)`. `lookup_control` / `encode_registered_control` / `encode_registered_release` take the registry. The module-level `CONTROL_REGISTRY` stays `()`, so truck-mcp's CLI, MCP and HTTP behaviour is unchanged. `_assert_candidate_is_honest` still guards every entry. |
| T-f | **Unsolicited `E3`.** Any frame from the module's rx ids carrying `7F AE E3 <r1> <r2>` (in heartbeat, request, release or sampler captures) does four things. (1) It marks every active control on that module `terminated_by_ecu`. (2) It burns all armed tickets. (3) It journals `ecu_terminated {module, reason: "<r1r2>", raw}`. (4) It leaves the lease open, so the operator still sees and controls the session. `ControlJournal.outstanding()` treats `ecu_terminated` as clearing every `(module, *)` activation. `describe_negative("31", "AE")` guidance gains "or a security-gated CPID (GMW3110)". |
| T-g | **Cross-process module lock.** `_module_lock` becomes a per-process `threading.Lock` plus an OS lock: `msvcrt.locking` on `%LOCALAPPDATA%\truck-control\locks\<module>.lock`, held for the lease's life. This prevents truck-mcp's CLI on the MX+ and OpenOBD on the GT from holding the same module at once. Today's lock is per-process only. |
| T-h | **Packaging.** `mcp` and `cantools` move to a `[server]` extra (the core needs only `pyserial`). Add `truck_mcp.__version__`. The README install line becomes `pip install -e ".[dev,server]"`. Tag `v0.12.x` (D4). |

**T-c in detail.**

`SpeedSource.sample(now) -> SpeedEvidence` must provide:

- **Primary:** PID `0D`, **two consecutive decoded reads**, each fresh, from
  distinct frames (different receipt times), both 0. A stale frame cannot
  stand in as a false zero (D3).
- **Corroborators** (EBCM wheel speeds): **refuse-only**. Any corner above 0
  refuses. A corroborator whose decode is not `calibrated` reports
  `not_examined` and never contributes to a pass. `FORBIDDEN_SPEED_SOURCES`
  still governs the primary.
- The existing channel-liveness proof.

`RangeSource.sample(now) -> Reading("range", value in {"P","R","N","D","3","2","1"} | None, ...)`
is judged as follows:

- When the profile says `range_mode = "sensed"`: the reading must be `ok`,
  must equal `P`, **and** the operator must attest Park. Any mismatch refuses
  and shows both values.
- When the profile says `range_mode = "attested"`: the Park attestation is
  required, and an available reading may only refuse (contradiction), never
  pass.

Battery stays 12.0 to 15.5 V.

### 4.5 Changes that land in **OpenOBD**

| # | Change |
|---|---|
| O-a | **`openobd/ctltransport_j2534.py`: `J2534Transport(RawFrameControlTransport)`.** Raw `CAN` at 500 kbps (HS; every Phase 1 entry is `bus: hs`). `pass_filter` for exactly the `rx_ids`. `LOOPBACK = 0`. `CLEAR_PERIODIC` at open. `READ_VBATT` for voltage. The device is opened once per lease and closed only after `close_session` (the OBDX driver may terminate the process at close, so nothing may be pending by then). It reuses `openobd/j2534.py`. |
| O-b | **`openobd/controls.py`: schema and registry builder.** It holds the closed-key-set schema for `controls.json` with a hand-written validator: no new dependency, unknown keys refused, types checked. New per-entry fields: `decoded_name` (null), `observed_result` (null), `release_id` (id of the captured release entry), `hazard` (truck-mcp `HAZARD_TIERS`), `tier` (0/1/2), `rpm_window`, `range_mode`, `dialect`, `max_hold_s`, `drive_capable`. **The builder leaves `decoded_name` and `observed_result` null and never invents them.** Names come only from Phase 0 (section 6.6). An entry is admitted to the registry only when every admission rule below holds. Admitted entries go through `control_candidate()` and then `verify_candidate(observed_result)`. The catalog's sha256 is journalled in `lease_open`. **With today's data the builder returns `()`.** |
| O-c | **Interlock providers.** `Pid0dSpeedSource` (two-sample). `EbcmWheelSpeedCorroborator` (refuse-only; `not_examined` until truck-mcp's `wheelspeed` mapping is calibrated). `TcmRangeSource` (returns `undecoded` until the PRNDL decode is established, so every `sensed` profile refuses). Interlock reads use the lease's own transport (one owner). |
| O-d | **`openobd/actuation.py`: the only module that imports `truck_mcp`.** It contains the flag (section 5), `LeaseWorker`, adapter arbitration, the journal path, the startup outstanding check and Recover. It is the only module allowed to call `ControlLease` / `arm` / `fire` / `release` / `activate`. Details below this table. |
| O-e | **`openobd/actuationui.py`: the Arm/Fire dialog, countdown, Abort and Release-all panel (section 6).** `app.py` composes it beside the unchanged `ActiveTestsPage` only when the flag is on. It uses theme tokens only. Its toolbar `actuationToolbar` joins `theme.TOOLBAR_NAMES` (the v0.39.1 coverage test enforces this). |
| O-f | `ctljournal.control_root_candidates()` union includes OpenOBD's journal root. Its `_fold` treats `ecu_terminated` exactly as truck-mcp's `outstanding()` does. A shared fixture file proves the two folds agree. |
| O-g | `openobd.spec`: add `('openobd/data/controls.json', 'openobd/data')` to `datas`, and the `truck_mcp.*` core modules to `hiddenimports` (section 7). |

**O-b admission rules.** All must hold:

- (a) `decoded_name` and `observed_result` are non-null.
- (b) The paired `release_id` is a captured, positive, mask-cleared same-CPID
  release or `AE00` on the same module. A "commanded OFF" entry with a
  non-zero mask is **not** a release.
- (c) `tier` is in `ADMITTED_TIERS = (0,)` at ship.
- (d) `drive_capable` is false, or the entry is in the empty whitelist.
- (e) `hazard` and `rpm_window` are set.

**O-d in detail.**

- **`LeaseWorker`** is a dedicated `QThread` that owns the transport and the
  lease for the whole session. It pumps every 0.1 s and services a command
  queue: arm, fire, release, release-all, abort. Abort is queued at top
  priority. The GUI never pumps the lease. A modal dialog or a stalled event
  loop must not be able to starve the heartbeat. If the worker itself wedges,
  the module's P3C timer is the backstop.
- **Adapter arbitration.** Opening a lease requires several things. Dashboard
  GT polling must be stopped and its serial port closed (`transport.py`
  `run_exclusive` / `stop`). No Diagnostics job may be running. No recording
  may be in progress (the open is refused with a reason rather than stopping
  the recording silently). Every other GT user is refused while a lease is
  held.
- **Journal path:** `ControlJournal(path=%LOCALAPPDATA%\OpenOBD\Logs\control\control-journal.jsonl)`.
  The path is always explicit. truck-mcp's default would resolve beside
  `openobd.exe` when frozen, and `TRUCK_MCP_DATA` would make an environment
  variable a control channel (SC-12). The path sits under OpenOBD's per-user
  log root, but **not** under the movable `logs/dir` setting, for three
  reasons. A user who changes the folder would orphan an outstanding
  activation. A OneDrive-redirected Documents folder can turn the fsynced file
  into a cloud placeholder or a sync-conflict copy. And a safety record must
  not share a lifecycle with recordings. The file is created empty when the
  flag is on, so the verdict starts as `clean` instead of `unknown`.
- **Startup.** If `ctljournal.control_state()` reports `outstanding` or
  `unknown`, Arm is disabled and the panel offers **Recover**. Recover is
  operator-initiated: `recover_indeterminate` with this journal, releases sent
  from journal bytes, then `AE00`, then `$20`, then read-back. Recovery never
  runs unattended at launch.

---

## 5. Feature flag

| Aspect | Decision |
|---|---|
| Name | `ACTUATION_BUILD_ENABLED` (code constant) **and** `activetests/actuation_enabled` (QSettings key) |
| Where | Both live in `openobd/actuation.py`. The single reader `actuation_enabled() -> tuple[bool, str]` returns `ACTUATION_BUILD_ENABLED and <key>`. The key is read through `appsettings.read_settings` (the chokepoint) |
| Default | Constant `False`. Key absent, which reads as off |
| Fail direction | A `SettingsReadError` reads as off, with the reason shown in the panel. An environment variable never enables it (SC-12) |
| Effect when off | `actuationui` is never constructed, and `truck_mcp` is never imported. A test asserts `"truck_mcp" not in sys.modules` after full `MainWindow` construction |
| What "the flag flip" means | The PR that sets `ACTUATION_BUILD_ENABLED = True` and bumps the version to **0.40.0**. The runtime key then lets Ron keep actuation off on any given machine |

**Why two layers.** A per-user registry value alone can be flipped without
review. The build constant makes enabling actuation a reviewable, gated code
change, which is what the load-bearing rule requires.

**Flip gate.** All of the following must hold, in this order:

1. `/security-gate` pass on every PR in section 8 that touches the mutation
   surface (cap 2 passes per branch).
2. `/security-audit` pass over truck-mcp `v0.12.x` (the pinned tag) and OpenOBD
   O1 to O6.
3. **First bench firing with Ron present.** This uses a build of the flip
   branch (unmerged) and Tier 0 entries only, and Ron's physical observation is
   recorded as each entry's `observed_result`.
4. Merge the flip PR. A gate finding blocks the flip: fix it and re-gate within
   the caps.

---

## 6. Safety

### 6.1 Two-step Arm then Fire

1. **Select** an admitted control. The panel shows `decoded_name`, module,
   tier, hazard, the exact request bytes and the exact release bytes.
2. **Arm** calls `lease.arm(name, hazard_ack=<typed words>, dry_run=False)`.
   It runs the full `_prepare_activation`: the registry, the profile and a live
   interlock gate. The GUI's `challenge_sink` is fixed at lease construction.
   It shows the six-hex challenge **only** inside the modal Arm dialog. The
   challenge is never logged, never in Copy report, never in a tooltip, and is
   cleared when the dialog closes.
3. **Fire** calls `lease.fire(ticket_id, typed_code)`. The ticket is burned on
   entry, the interlocks are **re-measured**, and the bytes must equal the
   armed bytes. Only then is the frame journalled (journal-before-frame,
   refused on a write failure) and sent.

### 6.2 Lease countdown

A live chip shows four values from `LeaseWorker` signals:

- lease remaining (out of 120 s);
- ticket remaining (out of 60 s);
- heartbeat age, which turns to warning above 1.0 s and danger at 1.5 s;
- per-control hold remaining (out of `max_hold_s`).

When the hold expires, the worker releases that control automatically.

### 6.3 Abort and Release-all

- **Abort** is always enabled while a lease exists. It closes the lease, and
  `close()` runs `_abort`, which never raises.
- **Release-all** releases every active control and keeps the lease.
- Both are one click with no confirmation, because stopping must never cost
  more than starting.
- **Mandatory release after every fire.** When an entry's `max_hold_s`
  expires, or the operator ends the test, the worker sends the entry's
  captured mask-cleared release (or `AE00`). The control's state returns to
  `released` only on a positive `EE`. If the release fails, the control stays
  `active` and the Arm button stays disabled.

### 6.4 Abort chain and adapter loss mid-fire

The order is unchanged from truck-mcp `_abort`. Every step is single-shot:

1. Each registered control's exact release (`$AE <cpid> <mask-cleared>`).
2. `AE 00` to each controlled module.
3. `$85 01`, only if a journal says DTC setting was suspended. Nothing in this
   wave can suspend it. DTC inhibition during `$AE` is the module's own
   behaviour (GMW3110), and it ends at `$20` or P3C timeout.
4. `$20` physically to each controlled module.
5. Adapter teardown (`close_session`), always last.

**If the GT is lost mid-fire** (USB unplugged, driver error, write or read
failure after bytes may have left):

- `send_payload` raises `ControlIndeterminate(sent=None)`.
- The lease journals `indeterminate {module, request, sent: null, detail}` and
  `session_lost`.
- `_abort` attempts steps 1 to 5. Each failure is recorded, and
  `abort_failed` / `escalation` is journalled.

The panel then shows **CONTROL STATE UNKNOWN** with this text:

> The module ends device control by itself within P3C (5.0 to 5.1 s, GMW3110
> Table 33) after the last heartbeat. Do not re-arm. Reconnect the GT and press
> Recover.

Arm stays disabled until Recover reads back `$20 -> 60` and the cross-root
verdict is `clean`.

### 6.5 Unsolicited `E3`

The GUI treats `E3` as "the module released every control". It:

- marks all of that module's controls `terminated_by_ecu`;
- shows the reason code as hex, labelled "GM reason code, not public";
- burns open tickets;
- keeps the lease, so heartbeat and Abort still work.

`E3` never requires a key cycle. The operator may re-arm only after the
interlock gate passes again.

### 6.6 Tier recovery and admission

- **Tier 0** (BCM 241, CPID 10/12/13 with `AE00`) is KOEO:
  `rpm_window = (0, 0)` and `range_mode = "attested"`. The engine cannot drive
  the wheels with the RPM window enforced at zero, and roll-away is covered by
  the parking-brake attestation plus two-sample speed. This is the D3 "profile
  declares no range signal" case.
- **Tier 1** (TCM 7E2, CPID 30/38) is engine-running: `rpm_window = (400, 1200)`
  and `range_mode = "sensed"`. It is **blocked until the PRNDL decode is
  established**, because `TcmRangeSource` returns `undecoded` and the gate
  refuses.
- **Tier 2** (TCCM 7E4 motor drive) carries the recovery procedure below. It
  is **not in `ADMITTED_TIERS` at v0.40.0.**

**Tier 2 recovery procedure.** It is required before Tier 2 can be admitted.

1. Before any drive, read and journal the actuator position and the selector
   position through `$22` DIDs proven readable
   (`didscan-7E4-supported.jsonl`). This needs no `$2C` and no periodic `$AA`,
   both of which truck-mcp refuses. If the position cannot be read, the drive
   is refused.
2. Send each drive command as the captured triple: drive
   (`AE01 0000 01B2 00` / `..0132..` per direction), then the enable-cleared
   form, then `AE01 00..00`. Each pulse is held for at most `max_hold_s`, which
   must not exceed the duration observed in the capture.
3. After the release, read the position back. If it is not within tolerance
   of the pre-drive baseline, the panel shows "TCCM actuator not at baseline"
   and refuses further TCCM fires for the session.
4. The re-learn (`7E4 AE03`) is excluded from v0.40.0 (D1). Both captured
   attempts answered `E3` with non-public reason codes, so the learn conditions
   are unknown. Establishing them in an Autel session is a Tier 2 admission
   prerequisite. Until then, recovery from a not-at-baseline actuator is the
   Autel's own learn, run by Ron.

**Phase 0** (prerequisite for any `decoded_name`): a listen-only `canrec`
capture while the Autel drives each menu item. Ron records the Autel menu label
and what physically moved. Those two strings become `decoded_name` and
`observed_result`.

### 6.7 Policy provenance

The Park, parking-brake, two-sample speed-0 and RPM-window interlocks are
**our conservative policy (D3 `e2c962dd`)**. They are not OEM-sourced language.
GMW3110 defines the module's own limits (`E3`), not the tester's
preconditions.

### 6.8 What is journalled

Every action and every refusal is journalled:

- Actions: `lease_open` (with catalog sha256, transport name and adapter
  identity), `armed`, `fired`, `activate`, `released`, `return_to_normal`,
  `ecu_terminated`, `indeterminate`, `session_lost`, `abort_failed` and
  `lease_closed`.
- Refusals: every `fire_refused` (expired, bad code, preconditions changed,
  frame changed). **New:** `arm_refused` and `lease_refused`, with the
  interlock snapshot and each failure string. Today a refused `arm()` or a
  refused lease open raises without a record.

Refusal records are best-effort (`append`). Records that precede a frame use
`_journal_or_refuse`.

---

## 7. Dependency and build

**Mechanism.**

- truck-mcp is pinned by **commit SHA**, with its tag in a comment
  (`truck-mcp @ git+https://github.com/Ohio15/truck-mcp@<40-hex>  # v0.12.x`).
  A tag is mutable and a SHA is not (SC-30).
- The pin lives in **`firstparty.txt`**, not `requirements.txt`. It is
  installed with `pip install --no-deps -r firstparty.txt` after
  `requirements.txt`. The core's only runtime dependency, `pyserial==3.5`, is
  already pinned there.
- **Measured 2026-10-27 with pip-audit 2.10.1:**
  - `pip-audit --strict --no-deps --disable-pip` fails on a URL requirement
    ("URL requirements cannot be pinned to a specific package version").
  - It also fails on any `==` pin of a package not on PyPI ("Dependency not
    found on PyPI and could not be audited").

  So a first-party package cannot sit in a `requirements*.txt` file without
  breaking `security-audit.yml`. truck-mcp's own `security-audit` covers its
  tree.
- A test asserts that `importlib.metadata` reports the installed truck-mcp
  version and `direct_url.json` commit equal to the `firstparty.txt` pin.

**PyInstaller.**

- `_build.bat` uses `.venv` as built per `docs/RELEASE.md`. That runbook gains
  the `firstparty.txt` install step.
- `openobd.spec` `Analysis` picks up `truck_mcp` through `openobd/actuation.py`'s
  static imports. `hiddenimports` lists `truck_mcp.control`, `.session`,
  `.ctltransport`, `.gmlan`, `.elm`, `.obd`, `.config`, `.paths`, `.enhanced`
  and `.monitors` explicitly, because the flag-off path never imports them.
- `datas` gains `controls.json`.
- `mcp` and `cantools` are absent with no extras installed, so nothing to
  exclude.

**BLOCKER: repository visibility (Ron's decision).** truck-mcp is **private**
and OpenOBD is **public**. Public CI cannot fetch the pin, and anyone else
cloning OpenOBD cannot build it.

- **Recommended: make truck-mcp public** after a full-history secrets and PII
  scan (gitleaks plus a VIN / serial / session-data review). Shared safety code
  benefits from being inspectable, and the AI-independence floor needs a
  rebuild from a clean clone.
- **Alternative: extract the control core** (`control`, `session`,
  `ctltransport`, `gmlan`, `obd`, `elm`, `config`, `paths`, `enhanced`,
  `monitors`) into a new public `Ohio15/gmlan-control` that both apps pin. It
  is cleaner if truck-mcp must stay private, but it costs a third repo and
  coordinated releases.
- **Rejected:** a read token as an Actions secret in this public repo's PR
  workflow. PR-controlled test code runs in the same job and could exfiltrate
  it (the SC-29 class).
- **Rejected:** committing a built wheel. That vendors private code into a
  public repo, and D4 rejects vendoring.

O1 waits on this decision. T-a to T-h do not.

---

## 8. Sequenced delivery plan

Each PR must stay green on its own. Each PR that touches the mutation surface
runs `/security-gate` before merge. "Inert" means no flag, no reachable fire
path, and an empty registry.

| # | Repo | PR | Version | Test it must carry |
|---|---|---|---|---|
| T1 | truck-mcp | Land `feat/control-foundation` (PR #5) to main, then tag (D4) | v0.12.0 | Existing suite green, CI, security-gate pass |
| T2 | truck-mcp | T-a + T-b: `ControlTransport`, `RawFrameControlTransport`, `StnRawTransport`, `ElmNoReplayView` rename, ISO-TP on `Frame` | v0.12.1 | **Golden transcript:** lease open / heartbeat / abort / recover produce byte-identical STN command and frame sequences before and after the refactor. Exported `transport_conformance.py` passes against `StnRawTransport` and a fake frame bus. MF TX with FC/WAIT/STmin fixtures |
| T3 | truck-mcp | T-c to T-g: providers, per-control profile, registry injection, `E3`, cross-process lock, `arm_refused` / `lease_refused` journalling | v0.12.2 | Pure gate truth tables: two-sample speed with a stale or identical frame, refuse-only corroborator, `sensed`/`attested` range, RPM windows, drive-capable whitelist empty. Mutation tests on each refusal. `E3` unsolicited in heartbeat capture leads to `ecu_terminated` and `outstanding()` clearing. A second process cannot take the module lock. `CONTROL_REGISTRY == ()` |
| T4 | truck-mcp | T-h packaging (`[server]` extra, `__version__`) | **tag v0.12.3** | CI job: clean venv, `pip install --no-deps .`, `pip install pyserial==3.5`, then `import truck_mcp.session, truck_mcp.ctltransport` |
| O0 | OpenOBD | **This ADR** (docs only, no version bump, matching #49/#50/#51) | none | CI green |
| O1 | OpenOBD | `firstparty.txt` pin, RELEASE.md step, spec `hiddenimports` + `datas`. **Blocked on section 7** | 0.39.2 | Installed truck-mcp commit equals the pin. The frozen-build smoke imports the core. `security-audit.yml` stays green (pin outside its glob) |
| O2 | OpenOBD | `openobd/controls.py` schema, validator and admission. `controls.json` gains null `decoded_name` / `observed_result` plus a profile with no names invented | 0.39.3 | Every current entry is non-admissible and the builder returns `()`. Admission truth table on fixtures. A "commanded OFF" non-zero-mask entry is rejected as a release. Unknown key or type is refused. sha256 is stable |
| O3 | OpenOBD | `J2534Transport` (O-a) | 0.39.4 | truck-mcp `transport_conformance.py` against a fake J2534 DLL. AST test: no `PassThruStartPeriodicMsg`. `CLEAR_PERIODIC` at open, `LOOPBACK = 0`. Any write error after send raises `ControlIndeterminate`. **Bench step (Ron present, zero actuation):** Stage 0 `$3E` + `$20` on 241 / 7E2 / 7E4 to measure heartbeat round trip against the 0.35 s / 1.5 s budget (the D2 revisit trigger) |
| O4 | OpenOBD | Interlock providers (O-c) and `ctljournal` union + `E3` fold (O-f) | 0.39.5 | Fold parity against a shared fixture with truck-mcp. Corroborator `not_examined` never passes. `undecoded` range refuses `sensed` |
| O5 | OpenOBD | `actuation.py` (O-d): flag, `LeaseWorker`, arbitration, journal path, Recover | 0.39.6 | Flag off leaves `truck_mcp` absent from `sys.modules`. A settings read error reads as off. An environment variable cannot enable it. No `ControlJournal()` without `path=`. Arbitration refuses while polling, a job or a recording is active. Worker heartbeat survives a blocked GUI thread. Recover is operator-initiated only |
| O6 | OpenOBD | `actuationui.py` (O-e): Arm/Fire dialog, countdown, Abort / Release-all, Recover | 0.39.7 | See section 9. Theme raw-colour grep. `actuationToolbar` in `TOOLBAR_NAMES`. The challenge is absent from the report, clipboard, logs and journal. Abort is enabled in every lease state. Manual test script via test-script-scribe |
| O7 | OpenOBD | **Flag flip:** `ACTUATION_BUILD_ENABLED = True`, README + CHANGELOG, AT-02a/b | **0.40.0** | Gate per section 5: security-gate, `/security-audit`, bench firing of Tier 0 with Ron present, `observed_result` recorded |

---

## 9. The read-only test: narrowed, not deleted

`tests/test_activetests_ui.py::test_read_only_no_control_actions` and
`_read_only_violations` stay **verbatim and keep auditing
`openobd/activetests.py`** with the flag on and with it off.

- Actuation lives in `actuation.py` / `actuationui.py`, which `app.py`
  composes next to the unchanged page.
- Nothing is added to `_RO_ALLOWED_IMPORTS`, and `_RO_BANNED_CALLS` keeps
  `ControlLease/arm/fire/activate/release`.
- The assertion `labels == ["refresh", "copy report"]` stays true, because the
  page's own toolbar does not change.

Retiring the test would remove the only proof that the journal viewer, which
is also the safety verdict, cannot actuate.

What changes behind the flag:

1. **New AST test:** `ControlLease` / `arm` / `fire` / `activate` / `release` /
   `recover_indeterminate` calls and `truck_mcp` imports appear **only** in
   `openobd/actuation.py`. This is an allow-list of one module.
2. **New test, flag off:** `MainWindow` has no `ActuationPanel` and
   `truck_mcp not in sys.modules`. **Flag on:** the panel exists, and its
   actions are exactly Arm, Fire, Release all, Abort and Recover.
3. **Manual test:** AT-02 splits.
   - **AT-02a (flag off):** today's text, P0.
   - **AT-02b (flag on):** no arm/fire path exists outside the Actuation panel,
     and none without the typed challenge.

   README's Active Tests paragraph is rewritten in O7, not earlier.

---

## 10. Threat model (for `/security-gate` and `/security-audit`)

**No network surface.** OpenOBD has no listener, the lease lives in-process,
and no HTTP, IPC or MCP route reaches `ControlLease`. The architecture C attack
surface does not exist.

**Who can attempt actuation.** The local desktop user only, behind three
independent gates:

- the build constant **and** the per-user key (section 5);
- the two-step Arm/Fire with a CSPRNG challenge shown only in the modal dialog;
- live interlocks re-measured at fire time.

A local process running UI automation in the same session is in the user's
trust domain and out of scope. It could equally drive the Autel.

**Untrusted inputs.** These are the inputs an adversary could shape:

- `controls.json`, which is bundled read-only in the exe, schema-validated
  with a closed key set, admission-gated and sha256-journalled;
- ECU replies, which are parsed by truck-mcp's NRC classes, with unknown codes
  never retried;
- the journal on reload, which goes through `assert_is_a_release(from_journal=True)`
  with bounded length and `$AE`/`$20` only;
- the settings store, where a read error means off.

**Sensor interlocks fail closed.** Unknown is never clean (SC-07). An
unestablished decode refuses. A corroborator can only refuse.

**Journal integrity.**

- Append-only and fsynced.
- Records that precede a frame refuse the frame if the write fails.
- The path is fixed and not influenced by the environment (SC-12).
- A torn last line is counted, and the verdict drops to `unknown`, which
  disables Arm.

The journal is **not** tamper-evident against the local user. A hash chain was
considered and rejected for v0.40.0: the attacker would be in the same trust
domain, and a broken chain must never block a release.

**Device-level dead-man.** No device-periodic `$3E` (rule 4.2.1). Process
death or a hang ends the control within P3C.

**Dependency.** Pinned by commit SHA (SC-30), with no install-time network
fetch at runtime.

**Register entries most relevant:**

- SC-03 (guard result gates the action)
- SC-04 (fail closed on I/O and parse errors)
- SC-07
- SC-12
- SC-18 (guard inputs at a weaker level: the interlock providers)
- SC-20 (source-scan guard spellings: the new AST tests)
- SC-30

---

## 11. Open questions

These are only the questions that cannot be resolved from code or data on this
seat.

1. **Repository visibility** (section 7). *Why open:* it is Ron's decision
   about publishing truck-mcp, or about extracting a public core. It blocks O1.
2. **GT raw-CAN round-trip timing on Windows** against the 0.35 s heartbeat
   capture and the 1.5 s deadline. *Why open:* only measurable on the GT with
   the truck. Stage 0 in O3 (`$3E` + `$20`, zero actuation) measures it. It is
   D2's revisit trigger.

   **GT periodic-message support** is **not** an open question. Section 4.2
   rule 1 forbids device-periodic `$3E` whether or not the GT supports it.
3. **PRNDL signal source and decode**, which gates Tier 1. *Why open:* no
   decoder exists in either repository, and finding the frame needs a
   listen-only capture while the selector is cycled at standstill.
4. **EBCM wheel-speed calibration**, which gates the corroborator. *Why open:*
   truck-mcp `wheelspeed.py` keeps frame, byte and scale as hypotheses until a
   reference drive regresses them. C0035 means the LF corner never counts.
5. **CPID bit meanings** (`decoded_name` / `observed_result`). *Why open:* GM
   does not publish them (`824fafa5`). Phase 0 capture with Ron observing is
   the only source.
6. **TCCM learn conditions** (Tier 2 recovery). *Why open:* the two captured
   `AE03` attempts drew `E3` with GM reason codes that are not public.
7. **BCM CPID 10/12/13 hazard class.** *Why open:* the hazard tier depends on
   what physically moves, which is Phase 0 output.

---

## 12. Consequences

- One safety core serves both adapters. A fix to the interlocks or the abort
  path lands once and is pinned by tag and SHA.
- truck-mcp carries a stable public contract (`ControlTransport` plus the
  conformance suite). Changing it now needs a tag bump and OpenOBD re-pin.
- `devctl.py` stays as a Ron-run CLI until O7. After the flip, it is deleted
  in a follow-up PR (it is the rejected option B).
- v0.40.0 ships live with Tier 0 only, and only once Phase 0 has produced
  names. With today's data the registry is empty, so even a flipped build
  fires nothing. That is the correct state until evidence exists.
