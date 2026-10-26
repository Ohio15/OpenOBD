# Changelog

Versions before 0.18.0 are recorded in the git history (commit subjects carry
the version, e.g. `feat(delta): ... (v0.17.0)`).

## 0.39.0 — 2026-10-26

### Added
- **Log folder setting.** Settings → Log folder… picks the folder Dashboard
  recordings go to (created if missing, and checked with a real test write so
  a OneDrive folder that refuses files is caught when you choose it);
  Settings → Reset log folder to default goes back to Documents\OpenOBD\Logs.
  Settings → Preferences… shows the current folder with Change… / Reset to
  default. Load log opens in the log folder.
- If the chosen folder is unavailable when a recording starts, the recording
  goes to the default folder and says so: "Log folder unavailable: <path>
  (using default)" in the status line and the record button's tooltip. The
  tooltip and the post-Stop status name the folder the file was saved in.
- **Live Data: Open session….** A toolbar action opens a truck-mcp session
  file (*.tmsession.db) from any folder, without locking out a logger that
  is still writing it: OpenOBD never writes rows, though SQLite may create
  WAL sidecar files (-wal/-shm) next to the file. It appears at the top of
  the session picker as "file · <name>" (kept across Rescan while the file
  exists; opening it again re-selects it; if a Rescan later discovers it,
  the discovered entry replaces it). Opened files show live or archived
  state honestly: the same live / not advancing / archived chips as a
  discovered session, from the file's own data, plus an "opened file" chip
  and the full path in the note; Hold works while it is live and is ignored
  once it has ended. The arrow on the button lists the 8 most recent files;
  files that no longer exist drop off at startup. A file that is not a
  readable session shows "could not open <name>: <reason>" and the bound
  session stays. OpenOBD's own CSV recordings still open in Log Analysis.
- Live Data's "as of" time is the newest sample's time (the date too when it
  is not today), for every session, not the time of the last poll; a
  session with no samples yet says "no samples yet".
- A log folder setting with environment variables ("%USERPROFILE%\Logs")
  is expanded; one that is still not an absolute path is not used and the
  status says "Log folder must be an absolute path: <value> (using
  default)". The write check gives up after 3 s (a cloud-only OneDrive
  folder or a stalled share) and reports "timed out", using the default.

### Fixed (review round 2)
- Opening a second session file no longer drops the first from the picker:
  both stay across Rescan, and the one you picked stays bound with the
  picker showing it.
- A session with no samples yet (opened or discovered) reads "not
  advancing", never "live", until a sample arrives.
- On Windows a log folder without a drive or \\server\share prefix (e.g.
  "%FOO%\Logs" with FOO empty, which becomes "\Logs") is refused as not
  absolute instead of landing on whatever drive is current.
- Settings shows the log folder as it resolves (variables expanded), and an
  unusable setting shows the default folder plus why; Log folder… starts in
  the resolved folder.

## 0.38.1 — 2026-10-26

### Fixed
- The tooltip guard test now sees every ToolTipRole position: a role in the
  middle of the call (`QTreeWidgetItem.setData(column, role, value)`), a
  `{ToolTipRole: value}` roles dict, and `match`/`case` role arms; any other
  mention of ToolTipRole (a role kept in a variable, a negated test, a
  ternary) is flagged, and a `theme` / `tuningui` name rebound in the file is
  no longer trusted as the escaping helper's module.

## 0.38.0 — 2026-10-26

### Changed (Active Tests tab UX overhaul; cues from HP Tuners VCM Scanner, GM GDS2, Autel special functions)
- **Shared chrome.** Breadcrumb "Active Tests › Control journal" with a verdict
  chip (clean / CONTROL LEFT ACTIVE / journal unknown / read failed), journal
  and record counts and "Polling" while the tab is showing; the note reads
  "as of HH:MM:SS · searched <roots>". One 36 px toolbar: Refresh (F5, only
  while Active Tests shows), Copy report, a journal filter (Ctrl+F focuses,
  Esc clears; display only) and a Show selector (All entries / Activate +
  refused only / Last 24 h, remembered).
- **Verdict banners.** An outstanding control shows a danger banner listing
  module, CPID, release bytes and journal path, and states that OpenOBD will
  not send the release and truck-mcp recovery must be run. An unknown verdict
  shows a warning banner with the reason; a clean verdict shows no banner (the
  green chip carries it).
- **Journal table.** Newest first, columns When / Event / Module / CPID /
  Detail; the date is shown when the day changes; activate and refused fires
  in the danger colour, releases in the ok colour; "No journal entries" when
  there is nothing to list. A collapsible "Journal sources" panel lists every
  data root searched and whether it held a journal (and any damage).
- **Honest freshness.** "as of" is the last SUCCESSFUL read. A failed read
  keeps the previous table but flags it: a "read failed" chip, a danger banner
  saying the table is the last good read, and "last good read HH:MM:SS" in the
  note. An unsafe last-good verdict stays visible through a failed read.
- The page stays strictly read-only: no arm, fire, activate or release
  control exists in OpenOBD.
- **Stable table.** The 3 s poll rebuilds the journal table only when what
  it shows changed; a rebuild keeps the selected rows and the scroll
  position. The filter and Show selector apply to every journal entry before
  the newest-100 limit, a footer row says "showing N of M matching entries"
  when the list is cut, "No matches" replaces an empty filtered list, and
  Copy report says "Entries shown (N of M ...)".
- **Failed reads stay labelled.** After a failed read the Journal sources
  panel starts with "last good read HH:MM:SS (not current)", Copy report says
  "Verdict: read failed (last good: <verdict> at HH:MM:SS)", and a table that
  cannot be rendered counts as a failed read (the last good table stays).
- **Control-state verdict hardening.** A journal record whose timestamp is
  NaN, infinite or too large to order now makes the verdict "journal
  unknown" instead of being sorted unpredictably (which could have hidden an
  unreleased activation); a list / object module or CPID no longer breaks
  the verdict. Tooltips on elided breadcrumb text render literally.

### Internal
- ActiveTestsPage moved from `livedata.py` to its own module
  `openobd/activetests.py`; the old ad-hoc banner / alert stylesheets and
  their colour tokens are gone (the page uses the shared workspace theme).

### Fixed
- Tooltips that show calibration, session or file text render it literally
  (no rich-text injection); a guard test keeps runtime tooltips (widget
  tooltips, model tooltip roles) escaped.

## 0.37.0 — 2026-10-26

### Changed (Live Data tab UX overhaul; cues from HP Tuners VCM Scanner, EFILive Scan, TunerStudio / Torque)
- **Shared chrome.** Breadcrumb "Live Data › <session>" with chips for the
  view state (live / not advancing / archived / read failed), provenance
  (SIMULATED, PROVENANCE UNKNOWN), channel and graphed counts and "held",
  plus a muted "<VIN> · source · as of" note; the status line is gone. One
  36 px toolbar: session picker (same width budget, elide + tooltip, wide
  popup), Rescan (F5, only while Live Data shows), Fields…, the chart Window
  span (moved out of the chart pane), Hold and a tile filter (Ctrl+F focuses,
  Esc clears).
- **Hold.** Freezes tiles, table and chart for reading; frozen live tiles take
  the not-advancing palette ("held · not updating") and a warning chip shows.
  The sample cursors stay put, so un-holding catches up on everything
  recorded meanwhile. Archived drives cannot be held; a drive that ends while
  held is let go.
- **Tiles.** Name with the unit right-aligned, big value, state caption and a
  muted min / max footer of measured (fresh) samples since bind (new
  incremental TmSessionReader.range_after; carried values never count).
  Tiles sit in collapsible Engine / Fuel / Spark / Trans / Sensors / Other
  groups with counts (collapse persisted under livedata/groups_collapsed).
  The filter is display only: it never changes the field layout.
- **Failures and empty states.** Read failures (two in a row) and "Could not
  open session" are a danger banner; a session that fails to open no longer
  leaves its reader bound. No data root / no sessions use the shared empty
  state.
- **Graphed-channels table.** Compact rows (tree style, ROW_HEIGHT), colour
  swatch icon instead of the "■" glyph, columns Field | Value | Unit | Min |
  Max | Avg | Lane (stats over the visible window, Lane as a chip), header
  tooltips, an empty-state row "Drag a tile here to graph it" and a height
  that fits its rows. Drop semantics are unchanged.
- **Strip chart.** Time axis under the last lane (-m:ss back from the right
  edge when live, HH:MM:SS for archived drives; ticks cached per window),
  hover cursor with a colour-matched readout box whose values also show in
  the table's Value column ("@ Cursor") until the mouse leaves, and session
  events as faint markers. Fonts are built once instead of per paint.
- **Persistence.** Splitter (livedata/split) and the window span for live
  sessions (livedata/span); archived drives still open on "All". A window
  chosen on an archived drive applies to that drive only and is not saved
  as the live default. A tampered splitter value falls back to the default
  sizes.
- **Hold is honest everywhere.** A layout edit while held (dropping a tile
  on the table, hiding / moving a field) rebuilds the tiles, table and
  lanes from the frozen snapshot: no trace, tile or table value advances
  until Release, and a field newly added to the graph is backfilled only
  up to the hold. The table's Value column is marked "Value · held" in the
  held colour; a carried tile reads "held · carried" instead of a frozen
  age.
- **Incomplete traces say so.** If loading a newly graphed field's history
  fails, its Min / Max / Avg read "not loaded" in the read-failed colour and
  the chart stays captioned "read failed" until a later read loads the
  whole history; the trace is never shown silently partial.
- **Fixes.** Losing the truck-mcp data root now unbinds the session (the
  chart no longer stays captioned "live" with an open reader). An archived
  drive with no fresh numeric samples paints without a time axis instead of
  aborting the paint (clock labels never raise on out-of-range times).
  Hover repaints reuse cached decimated trace points; tiles restyle only on
  a state change; the min / max cursor advances past carried-only rows.
  Labels showing session-DB or error text (tile unit / value / caption,
  chart note, breadcrumb title / note / chips) render as plain text.

## 0.36.0 — 2026-10-26

### Changed (Diagnostics tab UX overhaul; cues from GM GDS2 / Tech2, HP Tuners VCM Scanner, Autel / Snap-on)
- **Shared chrome.** Both Diagnostics sub-tabs carry the Tuning breadcrumb
  ("Diagnostics › Module Map", "Diagnostics › Codes & Readiness") with chips
  and a muted note, and one 36 px toolbar with vector glyph icons (the emoji
  buttons are gone). F5 runs the sub-tab's primary action (Scan Modules /
  Read Codes); Ctrl+F focuses the code search, Esc clears it. While a GT job
  runs every job-starting action is disabled and a "Scanning…" /
  "Reading…" chip shows — one job at a time, so a scan and a read can no
  longer race for the same result handler.
- **Module Map.** Chips for DLC voltage, modules responding, not examined
  and "fault at: <segment/module>"; the legend is a row under the header;
  hover a module for name, status, bus, request/response ids and role. A
  module card (title + status chip, Bus / Role / Diagnostic address, a
  collapsible "Scan notes" list) replaces the plain text box, with an empty
  state before the first scan. Scan errors and the GT binary-mode advice are
  an inline danger banner; the failed-segment marker is drawn as a vector
  triangle.
- **Codes & Readiness.** DTCs are a tree grouped Stored / Pending /
  Permanent with counts (Stored open by default, plus any group holding an
  unknown), columns Code | Description | Module | Status, status as chips.
  Not-examined and incomplete rows keep their exact wording and stay muted
  italic with their own glyph; "No trouble codes" is still only said when
  every kind was examined. Module comes from the responder id; a code with
  no local description says so. Search filters code, description and module;
  Copy codes puts the visible rows on the clipboard as TSV. Readiness is a
  tile grid with Ready / Not ready chips and an "n/m ready" summary. MIL ON
  is a danger banner, MIL not examined a muted one; a link error is an
  inline banner instead of a dialog (the two Clear-codes outcome dialogs
  stay).
- **Layout.** Map/card and codes/readiness splitter positions persist under
  `diag/`; the Diagnostics page's minimum width stays under 700 px.

### Fixed (review round 1 + security gate pass 1)
- A result handler that raised (scan or codes) left the page busy forever
  with every action disabled and F5 dead. The busy state is now restored
  before the scan result is applied and again in a `finally`; the exception
  text is shown as an inline danger banner.
- Clear Codes captures the clear, the DTC re-read and the readiness re-read
  separately. A re-read that fails after the ECM accepted the clear no
  longer hides the clear outcome or leaves the pre-clear rows standing as
  current: the table shows not-examined rows and the banner names the failed
  step ("Clear confirmed by 7E8; codes re-read failed: …").
- The codes table and chips are refreshed before any Clear-codes outcome
  dialog opens, with the job still marked busy until they are, so the table
  is current while the modal is up and a Read cannot be overwritten by
  older data.
- Before a scan every module on the map is "unknown"; single-wire modules
  were painted "unreachable via this path" with no scan having run.
- Map hit-testing (click, tooltip) uses the layout for the current widget
  size instead of the last paint's, so it is right straight after a resize.
- An empty `incomplete` entry for a DTC kind no longer marks that kind
  "incomplete".
- Empty-state labels are plain text, so GT error text placed in them (e.g.
  "Readiness not examined — …") can never render as rich text.
- `GtJob` itself refuses a second job while one is running ("another GT
  job is still running"), so one-job-at-a-time no longer depends only on the
  Diagnostics page's own busy flag.

### Fixed (review round 2)
- If the GT worker thread could not be started (e.g. "can't start new
  thread"), the error escaped the slot without a result and the page stayed
  busy forever. The start failure now releases the link and is delivered as
  the job's result, so it shows as a banner with every action re-enabled; the
  worker delivers a result for every outcome, including a non-`Exception`
  raise.
- A job error with an empty message (e.g. `TimeoutError()`) shows the
  exception type instead of a blank reason.
- A "another GT job is still running" refusal no longer paints every module
  failed ("Scan failed at the tool link"); it shows a warning banner and
  leaves the map and codes as they were.
- A failed codes read no longer says "Codes read at HH:MM:SS"; the note says
  "Codes not read — <why>" until a read succeeds.
- If showing the re-read after a clear fails, the banner still carries the
  clear outcome ("Clear confirmed by 7E8. Couldn't show the result: …").
- If the clear request itself raised, the banner says "Clear outcome
  unknown — the ECM may have acted: …" instead of implying nothing changed.
- Module-map nodes never get a negative width at narrow sizes (they could
  invert and become unclickable); minimum 24 px.
- The page stays busy while a Clear-codes outcome dialog is open, so no job
  can start underneath it (the busy flag was being cleared before the
  dialogs, contradicting the code's own comment).
- When a Diagnostics job opened its own GT link and closing that link failed
  after the job had run, the close error replaced the result, so a confirmed
  clear was reported as "Couldn't reach the truck". The result is kept (a
  dict result gets a `link_close_error` note and the failure is logged); a
  close failure after a job that raised never masks the job's own error.

## 0.35.0 — 2026-10-26

### Changed (Tuning tab UX overhaul; cues from HP Tuners VCM Editor, EFILive, TunerStudio)
- **Design tokens (`openobd/theme.py`).** Colours, spacing and control heights
  (toolbar 36 px, Dashboard primary row 56 px) plus QSS helpers live in one
  module; the dark palette, Dashboard Record/Mark buttons, Diagnostics, Live
  Data and the strip chart read them. Outside `theme.py`, no module in
  `openobd/` other than `gauges.py` holds a raw colour literal (`#rrggbb` or
  `QColor(r, g, b)`); `gauges.py` keeps its dial artwork colours, and a test
  scans every other module for both spellings.
- **Navigator.** Search-as-you-type (Ctrl+F focuses, Esc clears, Down/Enter
  navigate), collapsible categories with counts, a changed-vs-stock dot per
  table and a persisted "Recent" group (8 tables). A breadcrumb header
  ("Category › Table") carries unit, HPT id and changed-cells chips and the
  table note.
- **Editor.** One 36 px toolbar replaces both control rows: Amount, = + − × ÷ %
  (grid keys too), Interp ↔ / ↕ / 2D, Smooth (3×3 mean, undoable), Revert
  selection to stock, Next Δ (F3), Copy / Paste as Excel TSV (one undo step,
  clipped to the grid, any non-numeric field rejects the paste), Heatmap and
  Diff vs stock (Off / Absolute / Percent). Log-overlay controls moved to a
  collapsible right panel whose state persists.
- **Grid.** Axis headers show breakpoints with units and the corner reads
  "Y \ X"; full-spectrum heatmap (blue-cyan-green-yellow-red, per table min..max)
  with contrast-aware text; diff mode shows the delta on a diverging
  blue/grey/red scale with the real value in the tooltip; a corner triangle
  marks changed cells in every mode; the tooltip gives X, Y, value, stock and
  Δ %; a readout strip shows n, min, max, mean and changed count for the
  selection.
- **Scalars.** Grouped by category (collapsible), searchable, category filter,
  Δ chip, the same numeric validation, undoable edits and revert, and the
  safety note as a muted banner.
- **Log Analysis.** Same breadcrumb + toolbar chrome, channels in a checkable
  tree grouped Engine / Fuel / Spark / Trans / Sensors / Other, a cursor
  readout column beside the plots and an empty state before a log is loaded.
- The Tuning page's minimum width is now under 1000 px (was 1136).

### Tests
- New `tests/test_theme.py` and `tests/test_tuningui.py` (colour-scale
  endpoints, navigator filter + Recent persistence, Ctrl+F / Esc, Smooth
  undo/redo, TSV round trip / clipping / rejection, diff colouring and
  tooltip, changed-cell marker, selection stats, scalar undo, minimum width).

### Fixed (review round 1 + security gate pass 1)
- nan / inf / -inf can no longer enter a calibration: one finite-number gate
  (`editops.parse_number`) backs paste (the whole paste is refused and the
  status bar says why), in-cell edits and scalar edits.
- Log overlay grids and their scale are built once per change instead of per
  cell per role (a 16x16 repaint rebuilt them over a thousand times).
- Copy / Paste only act while Tuning > Editor is showing; Ctrl+V on Scalars,
  Log Analysis or another page no longer writes into the hidden table.
- Undoing back to the saved state clears the unsaved-changes flag (the undo
  stack's clean index tracks save and load).
- Holding a grid math key (+, -, ...) no longer pushes one undo step per
  key repeat.
- Clicking a Recent table rebuilds the Recent group after the click is
  delivered, not while Qt still holds the clicked item.
- Opening another calibration with unsaved edits asks about (and saves) the
  OLD file before loading the new one; previously Save wrote the new file and
  the old edits were lost.
- A table with no stock baseline reads as such: no navigator dot, a muted
  "no stock baseline" chip and "n/a" diff text, never "0 cells changed".
- Diff colour scale tolerates a stock grid whose shape differs from the
  values grid. Dead `CalTableModel.revert_cell` (bypassed undo) removed.

### Fixed (review round 2 + security gate pass 2)
- Non-finite RESULTS are refused too: every table write (cell edit, math,
  smooth, interpolate, paste, revert) passes through `BulkEditCommand`, which
  rejects any non-finite new value before it becomes an undoable edit
  (repeated x1e9 overflowing to inf now reports "edit refused: non-finite
  result"); `ScalarEditCommand` applies the same `editops.parse_number`
  gate, and the scalar editor parses with it instead of its own check.
- The selection readout and the Scalars breadcrumb say "no stock baseline"
  instead of "0 changed" when there is nothing to compare against.
- The navigator icon is three-valued: changed dot, blank for unchanged, and
  a hollow muted ring for a table with no stock baseline.
- Pasting a TSV with an interior blank line (Excel's empty cell in a
  one-column range) keeps that row as blanks; rows below no longer shift up.

## 0.34.1 — 2026-10-26

### Fixed (release tooling, discovery 455efc83)
- **`_deploy.ps1` refuses a stale build.** On the v0.34.0 release a silently
  skipped build left a 0.32.2 `dist\openobd.exe` that the deploy installed over
  0.33.0. The deploy now checks, before touching anything (no copy, no backup,
  no install dir created), that `build.log` ends `BUILD_DONE_EXIT_0` then
  `VERIFY_DONE_EXIT_0`, that the exe `ProductVersion` equals `__version__`, that
  no file under `openobd\` is newer than the exe, and that the exe and
  `build.log` come from the same build (mtimes within 10 minutes). New
  parameters `-DistExe`, `-InstallDir`, `-BuildLog`, `-PackageDir`,
  `-NoShortcuts` default to the real locations.
- **`_build.bat` deletes the previous `dist\openobd.exe` and `build.log` first**,
  so a failed or skipped build cannot leave a stale exe behind.
- **`docs\RELEASE.md`**: build and deploy run from PowerShell or cmd, never Git
  Bash/MSYS (which mangles the `cmd /c` argument and skips the build), plus an
  explicit pre-deploy version check step.
- Tests: `tests/test_deploy_gate.py` runs the real `_deploy.ps1` against a
  throwaway dist and install dir.

## 0.34.0 — 2026-10-26

### Changed (Dashboard controls redesign)
- **GT auto-connect.** The Dashboard looks for the OBDX Pro GT's USB ID
  (0483:5740, pyserial list_ports, GUI thread, no port opened) when it is
  built and every 2 s while no live GT is bound, and connects through the
  existing worker-thread connect when it appears. It does not scan while a
  connect is in flight, while a GT is live (the poll thread re-opens a dropped
  link itself), or while the user has bound a truck-mcp session or a log
  replay. A failed connect shows in the status line and is retried on the
  next scan, after a backoff of 5 s, then 15 s, then every 30 s (reset
  when the port disappears or on Reconnect GT now), under one stable status
  line "OBDX Pro GT found on COMx but it could not be opened (<reason>) ·
  retrying". A 20 s watchdog marks a connect that never returns as failed
  and drops its late result. While nothing is found the status reads
  "Looking for OBDX Pro GT… (plug in the USB)". While a log or session is
  bound, a GT that appears adds "· OBDX Pro GT detected — Settings →
  Reconnect GT now to switch" to the status line instead of connecting. Any
  connect (automatic or Reconnect GT now) that finishes after the user bound
  another source, or chose Disconnect, is released, not bound.
  The scan is injectable (`Dashboard(ports_provider=, scan_interval_ms=,
  gt_factory=)`). The "Connect GT Pro" button is gone; **Settings → Reconnect
  GT now** (Ctrl+G) forces a connect, and **Settings → Disconnect GT** stops
  the GT (an active recording is finalized and its saved path stays in the
  status line). After a Disconnect the GT is not re-opened while it stays
  plugged in; unplugging it, or choosing Reconnect GT now, lifts that.
- **One big Record/Stop button.** Its own row under the status line: 56 px
  high, red "⏺  RECORD" when idle, "⏹  STOP  ·  1,234 samples  ·  02:14"
  while recording (elapsed from the first recorded sample, on the source's
  clock). Clicking it while recording stops and finalizes as before (offers
  Log Analysis). Only a live GT records: the button is disabled, with the
  reason as its tooltip, for nothing bound, a disconnected GT, a truck-mcp
  session, or a log replay ("Recording needs a live GT; this is a replay").
  Styled by a stylesheet on the button only.
- **Mark beside it**, same height, shown only while recording (M still
  marks).
- **Replay transport.** Start and Stop moved into the replay strip with
  Pause, Speed and the position slider. The strip shows for log replays and
  truck-mcp sessions (Pause/Speed/slider only for seekable replays); the live
  GT has no Stop button and F5 / Shift+F5 never touch it.
- **Truck session picker** moved to **File → Open truck-mcp session…**
  (Ctrl+T). Simulated sessions are prefixed "[SIM] "
  (`tmstore.session_tag(entry, short=True)`), under a disabled header
  "Stored sessions — not the truck; recording needs a live GT". The Record
  refusal for a truck-mcp session now says to plug in the GT or choose
  "Settings → Reconnect GT now".
- **Keys:** R toggles record (Ctrl+R still works), M marks, Space toggles
  pause on a replay (P still works), listed in the status line's tooltip.
- **Fixed: samples lost at stop.** Every stop path (replay Stop,
  Disconnect GT, binding another source, app close) finalized the recording
  BEFORE stopping the source, and GtDataSource.stop() joins the poll thread
  for up to 3 s while it keeps publishing; those samples never reached the
  CSV. The source now stops first, then the final drain and close run
  (`Dashboard._halt_source`). A recording-start vehicle read still in
  flight is waited for (up to 3 s) and written into the recording before it
  closes. GtDataSource.stop() never closes the port under a run_exclusive
  holder: if the IO lock is not free within its bounded wait, the holder
  closes the port as it lets go.
- A connect worker that is still blocked after the watchdog fired holds off
  any new connect ("previous GT open still hung; waiting for it to
  return"), so hung opens never pile up. Unplugging during a connect cancels
  it and resets the backoff; binding a log or session cancels a connect in
  flight, and its failure or timeout never touches that source's status.
- Tests: `tests/test_dashboard_controls.py` (fake ports provider, fake GT
  factory, hand-driven scan ticks, virtual clock); `tests/conftest.py` makes
  the default ports provider report no GT so no test can open real hardware;
  the GUI smoke does the same and checks the new menu actions.


### Added (CI runs the test gate)
- `.github/workflows/tests.yml` runs the docs/RELEASE.md step 3 gate,
  `python -m pytest tests/ -q` then `tests/smoke_gui.py` offscreen, on every
  PR and push to main, on windows-latest and ubuntu-latest with Python 3.13
  and requirements.txt's pins. Until now pytest and the smoke ran only in the
  local pre-commit hook. A `Tests summary (always runs)` job fails unless
  every leg succeeded and is the context to mark required.
- `tests/smoke_gui.py` wrote its fixture log and save round-trip file to a
  literal `/tmp/...`, which on Windows is `	mp` on the current drive and
  only worked where that directory existed. Both now go in the smoke's
  per-run temp tree.

### Fixed (flaky GUI smoke: GT freshness on wall time)
- `tests/smoke_gui.py`'s GT poll-health and gauge-fluency sections judged
  reading freshness on the real `time.monotonic()`, while the fluency section
  also waits up to 1 s of wall time for the needle animation. On a busy
  machine the held MAF reading aged past its 0.5 s window (2 x the 0.25 s
  cycle) and the smoke reported "stale" where "fresh" was expected (seen
  once on 2026-10-25; a 0.6 s stall reproduces it every time on the old
  smoke). `GtDataSource` now takes `now`, the monotonic clock it keeps
  connect time, reading ages and freshness on (default `time.monotonic`, the
  clock ObdxGt stamps readings on; the app passes nothing, so behaviour is
  unchanged). The smoke and `tests/test_gt_dashboard.py` inject the virtual
  clock now shared from `tests/vclock.py` (the one `test_sample_timing.py`
  already used), and the smoke drives it to either side of maf's window
  (fresh 1 ms inside, stale 1 ms past) instead of shifting `_t0` by 100 s.

### Fixed (flaky test: truck-mcp live-follow freshness on wall time)
- `tests/test_tmsource.py`'s live-source tests stamped `channel_latest` with
  the real UTC time (truncated to whole seconds) and then asserted "fresh",
  while `TmLiveSource` aged the session's newest stamp against
  `datetime.now(UTC)` with `tmstore.STALE_SESSION_LIMIT_S` = 5 s, so a stall
  of 4 to 5 s between the write and the assertion flipped `rpm` to "stale"
  (brain 7379e172). `TmLiveSource` now takes `wall_clock`, the epoch-seconds
  clock it ages that stamp against (passed to `tmstore.sample_age_s(now=)`;
  default `time.time`, the clock truck-mcp stamps `ts_utc` on; the app passes
  nothing, so behaviour is unchanged). Read cadence and `Sample.t` stay on
  `time.monotonic`. The tests use a fixed store stamp and the virtual clock
  from `tests/vclock.py`, and `test_live_source_converts_and_states` now
  drives it 1 ms inside the limit (fresh) and 1 ms past it (stale, value
  kept, carried unchanged), so the session-stale rule is still exercised.

## 0.33.0 — 2026-10-25

### Added (recording metadata sidecar)
- **Every Dashboard recording now has a `<stem>.meta.json`** beside
  `<stem>.csv` and `<stem>.long.csv` (`recorder.META_SUFFIX`, schema 1). It
  is created exclusively with the other two (flushed, fsynced; a stray
  `.meta.json` moves the recording to the next free stem) and rewritten
  atomically (temp file, fsync, `os.replace`) when a marker or a vehicle fact
  is added and at close. An empty recording removes all three files.
  Contents: schema, `openobd_version`, `utc_start` / `local_start` and the
  end times, file names, columns, the source (GT device, port, poll plan
  summary, poll cost model), vehicle identity, the four Mode 01
  supported-PID masks as hex plus the decoded PID list, DTCs at start (the
  full `read_dtcs()` dict), markers, the pedal calibration in force and its
  changes, channel notes, derived channels, rows, duration, error.
- **Vehicle identity from Mode 09** (`ObdxGt.read_vehicle_identity`): VIN
  (09 02), CALIDs (09 04) and CVNs (09 06) from the ECM, physical 7E0. Each
  entry holds the raw message hex plus decoded text where applicable, or
  `{"examined": false, "why": ...}` (NO DATA, negative response with NRC,
  count byte not matching the data, a VIN that is not 17 VIN characters).
  Never an empty string standing in for a value. `gt.service_message`
  generalises the wave-1 multi-frame parser to any service; a `7F <svc> 78`
  (response pending) is skipped and the final reply in the same read is
  used, and a read holding only the 78 is "not examined (response pending,
  no final reply)". Verified
  against the real 7E8 replies truck-mcp captured on this truck (VIN
  3GCRKTE35AG150432, 8 CALIDs); the headers-off GT text form is proven
  only against the fake until the truck is plugged in.
- **The VIN is read on every open** (connect and link re-open; one 0902
  request, `ObdxGt.read_vin`, before polling resumes) and published as
  `GtDataSource.link` = (connection epoch, VIN entry). **CALIDs, CVNs and
  DTCs are read at recording start**, with the poll thread held
  (`GtDataSource.read_recording_context` via `run_exclusive`) on a worker
  thread, so the GUI never blocks; the connect-time VIN is reused. The
  identity is cached per connection and dropped on every open and on stop;
  DTCs are read per recording. After `read_dtcs` the poll state is back to 7E0 / ATH0 and
  the next poll goes straight out (tested with the fake).
- **Markers**: `M` (or the new Mark button) while recording drops "mark N"
  at the current recording time into the metadata and the status line. The
  CSVs are untouched. Log Analysis draws a recording's markers as amber
  event markers (the existing "Event markers" checkbox) and lists them in
  the report.

### Added (pedal normalization)
- **A derived "Pedal, normalized (%)" channel** (`openobd/pedal.py`): clamp
  ((APP D - rest) / (full - rest) * 100, 0, 100). On the 2026-10-25 drive
  APP D read 19 % at rest and 61 % flat out, so `pedal >= 70` found no WOT.
  rest = the MEDIAN APP D over a stretch with rpm < 1200 and vss == 0 held
  for 2 s of OBSERVED samples (broken by a missing value, a failed poll, or
  a gap over twice the longest normal cycle; a log uses twice its own median
  spacing), only lowered; full = the maximum APP D over a run of at least 3
  consecutive samples with load >= 98 % or MAP >= 95 kPa and the engine
  running (rpm >= 400, because a key-on engine-off MAP reads barometric),
  only raised. A single glitch or spike moves neither. Until both are
  learned the channel is blank. The raw `app` / `app_e` / `app_f` channels
  are recorded exactly as decoded.
- Computed in `GtDataSource` on the poll thread (`Sample.derived`), so the
  display and the recording both get it. In the long sidecar its lines carry
  the APP D reading's time and `derived:app` in the Request column, never a
  request number.
- **A calibration is stored and applied only for an examined VIN**
  (`pedal_cal/<VIN>`; there is no fallback key). Every open (connect,
  re-open after a link loss) and stop resets the learner and the VIN, so a
  calibration learned on one truck is never applied to or saved under
  another; the connection's own VIN then seeds its stored calibration, so
  the live gauge is normalized before any recording. Without a VIN the
  calibration is session-only (live learning still works) and the metadata
  says so. If the stored calibration cannot be READ, that is not "none
  stored": nothing is seeded and nothing is saved over it that session, and
  the status line and metadata say so. Calibration changes, markers and
  link re-opens (`link_events`) are written into an active recording's
  metadata within 250 ms (rewrites are coalesced so a held M key cannot
  stall the GUI on fsync); close always writes the final document.
- **Log Analysis → "Learn pedal calibration from this log"** derives it
  from a loaded log with the same rules (`logbin.calibrate_pedal_from_log`;
  the 2026-10-25 log gives rest 19.2 / full 61.2). For a log with a VIN it
  is stored only when the log's VIN equals the VIN examined on the current
  live GT connection, merged into the stored one (rest = min, full = max:
  never loosened), and the status line shows old and new. Otherwise (no
  VIN, no connection, another vehicle, unreadable store) it applies to that
  log's analysis only. A loaded log uses only the calibration its own
  recording measured (latest change, then after-identity, then at-start).
  A VIN in a .meta.json is shown as "from file, unverified" and never
  loads stored settings: the file is editable on disk (security gate
  SC-05).
- `.meta.json` shapes are validated before use (vehicle, VIN, pedal
  calibration, markers); a hostile or damaged file is reported as
  "metadata unreadable: <why>" and the log loads without it.
- The **Pedal gauge** shows the normalized value when available, else raw
  APP D labelled "Pedal raw" with "pedal cal pending" in its tooltip.

### Changed (WOT / PE detection)
- `analyze_log` and `detect_shift_points` use the normalized pedal when the
  log has it; raw APP D only when a pedal calibration is known for the log
  (from its metadata, the VIN's stored calibration, or Learn); otherwise
  load >= 98 % or MAP >= 95 kPa, and the report says so ("pedal
  uncalibrated; WOT inferred from load/MAP"). In that case running non-WOT
  samples are one "off-idle (pedal uncalibrated)" regime: cruise and
  part-throttle cannot be split without a pedal. Raw TPS no longer stands
  in for the pedal. The report prints the WOT criterion and the WOT/PE
  sample count (2026-10-25 log: 0 before, 50 under the fallback, 14 with
  the learned calibration; 13 of the 14 are also fallback samples, the
  14th is the pull's tip-in, row 193, where the pedal led load and MAP).

### Changed (ethanol)
- PID 52 is labelled as the ECM's estimate everywhere: column "Ethanol, ECM
  estimate (%)", gauges "EtOH est" / "ETOH EST", and a channel note in the
  metadata. It is still recorded. The old "Ethanol (%)" header still loads
  as ethanol.

### Changed (GT polling: batched and tiered)
- **The GT now asks for up to six Mode 01 PIDs per request and polls in
  tiers** (`openobd/pollplan.py`, ported from truck-mcp `sweep.py` /
  `obd.py`). The 2026-10-25 drive polled 22 PIDs one request at a time,
  ~63 ms each: 0.69 Hz, too slow for shift analysis and WOT pulls. FAST
  (rpm, vss, map, maf, tps, app, app E/F, spark, load, abs load, STFT B1/B2,
  relative TPS, commanded throttle, EQ ratio, fuel pressure) is asked every
  cycle, MEDIUM (LTFT B1/B2, four O2 voltages, fuel system, IAT, ECT) every
  4th, SLOW (baro, ambient, fuel level, ethanol, voltage, run time, cat temps)
  every 20th; cycle 0 asks for everything. Only supported PIDs are planned,
  and the plan is rebuilt when the supported set changes (reconnect). For
  this truck's 34 supported PIDs the plan predicts 3 requests / ~231 ms per
  fast cycle (4.3 Hz) and ~270 ms mean (3.55 requests/cycle) from the
  measured costs (1 PID 55.7 ms, 3 PIDs 55.0 ms, 6 PIDs 79.9 ms; truck-mcp
  cassette no-accel-1000rpm 2026-09-18). The plan is shown in the Dashboard
  status line at connect and logged (`openobd.gt` / `openobd.transport`
  loggers). Polling runs back to back (`poll_interval` 0); a 50 ms pause
  follows a failed poll.
- **A proper multi-frame reply parser** (`gt.mode01_message` /
  `parse_mode01_reply`). The ELM's headers-off multi-frame text (a 3-digit
  length line, then `N:` frame lines) is joined by frame index, cut at the
  declared length (drops padding) and walked by (PID, byte length) in the
  order the ECM answered. The old hex-strip `_extract` would have read the
  length line and the indices as data. A PID left out of the reply gets no
  reading; a headers-on reply (failed ATH0 restore) is reassembled per
  responder. `PID_TABLE` entries are now `PidDef(key, nbytes, decode)`
  (PID 44 is 2 bytes per SAE J1979; the 4-byte PIDs are 24-2B and 34-3B).
  A sidecar whose last line has no newline (cut off by a crash mid-write)
  drops that line instead of reading a truncated value.
- **A batch the ECM answers with nothing is re-asked one PID per request in
  the same cycle.** If the singles answer, that cycle refused the batch.
  Only `ObdxGt.REFUSAL_CYCLES` (2) consecutive refusing cycles set
  `ObdxGt.batching_refused` (logged with the count) and drop the plan to one
  PID per request until the next connect; a single lost frame or CAN ERROR
  does not cost the ~6x throughput, and an answered batch resets the count. If they do not, the ECM is quiet (key
  off) and the rest of the cycle is not re-asked.
- **One Sample per cycle with only that cycle's readings.** A not-due
  channel is a blank cell in the recording and has no sidecar line; nothing
  is carried forward into a recording. Values from one batched reply share
  its request number and exchange times. The ATRV voltage fallback runs on
  SLOW cycles (and on any cycle where nothing decoded, so a quiet ECM is not
  a dead link) and is numbered like any request. Mode 22 DIDs are polled on
  MEDIUM cycles.
- **The Dashboard reads each GT channel's last reading, not the newest
  Sample.** `GtDataSource` keeps a per-channel latest (`channel_values()`,
  `channel_ages()`); `channel_states()` says `fresh` while a channel's last
  reading is within twice its tier period (measured cycle time), `stale`
  after, `failed` while polls fail, and nothing for a channel that never
  answered (it used to claim `fresh` for every key). This replaces
  `gaugemotion.LiveFreshness`, which judged freshness from the newest
  sample and would have blanked every MEDIUM/SLOW gauge between refreshes;
  it is removed with its tests.

### Added
- **New GT channels**: STFT/LTFT bank 2 (PIDs 08/09), O2 sensor voltage
  B1S1/B1S2/B2S1/B2S2 (14/15/18/19, byte0/200 V), catalyst temp B1S1/B2S1
  (3C/3D), fuel system status (03, raw code), absolute load (43), relative
  TPS (45), pedal E/F (47/4A). All are in this ECM's supported-PID bitmap
  (2026-10-25 probe). Each has a recording column (`RECORD_LABELS` /
  `RECORD_ORDER`) and a `logbin.CANONICAL` entry; bank-2 trims, upstream O2
  volts (read to 0.01 V) and cat temps have gauges in both clusters.
- **Log Analysis reads the `.long.csv` sidecar** when it sits next to a
  loaded CSV (`logbin.parse_long_csv` / `attach_long`, `Log.timed`). Regime
  trims classify each STFT/LTFT reading by the rpm and pedal nearest its own
  time, and shift detection walks rpm readings at their own times; without
  a sidecar both are unchanged. The report says when the sidecar was used.

### Changed (channel mapping)
- `"absload"` now maps to the new `abs_load` channel instead of `load`:
  PID 43 absolute load is a different quantity from calculated load and can
  exceed 100 %. New needles are ordered so they cannot capture an existing
  channel's label (checked by tests).

### Fixed (dependency audit)
- **`requirements.txt` is now pinned to exact versions** (`PySide6==6.11.2`,
  `pyserial==3.5`, `pyqtgraph==0.14.0`, `numpy==2.5.2`, `pytest==9.1.1`,
  `pyinstaller==6.22.2`), the versions installed in the `.venv` the release
  exe is built from, so the build is unchanged. The PR-side Security Audit
  runs `pip-audit --strict --no-deps --disable-pip -r requirements.txt`,
  which rejects any entry not pinned with `==`; the `>=` ranges failed every
  pull request since the guard re-sync in #42, while pushes to main (which
  resolve the graph instead) stayed green. Both modes now pass with no known
  vulnerabilities.

## 0.32.3 — 2026-10-25

### Fixed (truck-mcp session provenance)
- **A drive run against the truck-mcp simulator is no longer shown as a real
  drive.** truck-mcp v0.11.0a9 writes an authoritative `session.source`
  (`live` / `simulated` / `replay`) and `extra.simulated = true` on simulated
  sessions. `source == "simulated"` and `extra.simulated is True` are now the
  primary signals; the old `sweep_config.started_by == "seed_demo_data"` and
  notes-prefix `SIMULATED` markers remain as fallbacks for older files.
- **Provenance is three-valued (simulated / real / unknown), closing security
  gate finding SC-07.** A corrupt `sweep_config` or `extra` column, or a
  `session.source` value this reader does not recognise, can no longer render
  as "real". It is tagged `[PROVENANCE UNKNOWN]` in the pickers, the Live Data
  status line says "PROVENANCE UNKNOWN, may not be the truck", and the Record
  refusal names it as an UNKNOWN-PROVENANCE session instead of calling it a
  replay. `tmsource.record_refusal_message` now takes a provenance string, and
  session sources expose `provenance` in place of the `simulated` bool;
  `tmstore.list_sessions()` entries carry both `provenance` and `simulated`.

## 0.32.2 — 2026-10-25

### Fixed (recorded times say when each value was sampled)
- **GT recordings no longer misdate their values.** The poll loop stamped a
  whole sequential sweep (21 Mode 01 PIDs + ATRV/DID, ~63 ms each, ~1.4 s)
  once, AFTER it finished, so on the 2026-10-25 drive engine load was 1.33 s
  older than its row time, RPM 0.95 s, MAF 0.70 s, and pedal was stamped
  newer than throttle. Every request is now timed from just before it is
  written to just after its reply is read (`ObdxGt.poll_timed()` returns
  `transport.Reading`s carrying those times and a request number), and the
  values keep those times through `Sample.readings`.
- **The wide CSV keeps one row per sweep** (the regime report, table overlay
  and shift detection read it unchanged; a channel that did not answer in a
  sweep is a blank cell, read as missing). Its `Time (s)` is now the **sweep
  midpoint**, a documented reference within half a sweep of every value in
  the row, instead of the sweep end. Time is written to the millisecond.
- **New lossless sidecar `<stem>.long.csv`** beside each recording: one line
  per value with its own request midpoint, channel, full-precision value,
  request number, and the sweep's start and end, so the row's skew can be
  bounded. Written with the CSV's durability (flush every batch, fsync at
  most every 2 s, finalized on stop/close/app exit, removed with an empty
  recording, never paired with another run's file). Log Analysis does not
  read it yet; that follows when PID batching / tiered polling land.
- **`Time (s)` starts at 0 at the first recorded sample,** not at GT connect
  (the 2026-10-25 log's first row read 8.39 s). The sidecar shares that
  zero, so the first sweep's earliest requests sit slightly below 0.

## 0.32.1 — 2026-10-25

### Fixed
- **The GT no longer shows a constant as live trans fluid temperature.**
  ECM DID 1644 byte1, polled as `tft` ("Trans Fluid Temp (F)") since
  commit ee0189f (2026-08-18) on the strength of one match to the DIC, read raw
  0x83 (195.8 F) on all 1463 rows of the 2026-10-25 drive while coolant rose
  127 -> 203 F, and 0x0083 in every `tools/snap_*.tsv` snapshot. It is moved
  from `gt.DID_TABLE` to `gt.REJECTED_DIDS` (never polled, evidence
  recorded). On the GT path the Trans gauge is now dimmed and new recordings
  have no "Trans Fluid Temp (F)" column. Logs replayed from HP Tuners and
  truck-mcp sessions still feed the gauge from their own sources. Note:
  "Trans Fluid Temp (F)" in OpenOBD GT recordings made before this version is
  that constant, not a measurement.
- The candidate source, TCM (7E2 -> 7EA) DID 1940, is unproven and is NOT a
  channel; `gt.py` documents the drive test that must pass before it is
  added to `DID_TABLE`.
- `_deploy.ps1` failed to parse under Windows PowerShell 5.1 (the runbook's
  `powershell -File` path): the BOM-less UTF-8 em dashes were read as ANSI.
  The script is now ASCII; `tools/extract.ps1` (which needs degree signs)
  carries a UTF-8 BOM. `tests/test_script_encoding.py` keeps every tracked
  script ASCII-or-BOM.

## 0.32.0 — 2026-10-25

### Fixed (drive logging from the OBDX GT)
- **Record was refused for the GT itself.** The truck-mcp guard in
  `toggle_record` was `hasattr(source, "channel_states")`, which
  `GtDataSource` has matched since v0.18.4, so every GT recording got the
  "use truck-mcp session_export" refusal. Sources now declare
  `is_store_record` (True only for `TmReplaySource` / `TmLiveSource`).
- **Recordings are durable from the first row.** New `openobd/recorder.py`
  writes the final CSV straight into `Documents\OpenOBD\Logs` (Qt known-folder
  lookup, so a OneDrive-redirected Documents works; falls back to
  `%LOCALAPPDATA%\OpenOBD\Logs`). Header written at start, every batch
  flushed, fsync at most every 2 s, exclusive file names. No `%TEMP%` file, no
  end-of-drive save dialog to cancel; a crash keeps everything written, a
  power cut loses at most ~2 s.
- **Closing the app mid-drive finalizes the recording** and releases the GT
  port (`Dashboard.shutdown()` from `MainWindow._shutdown_pages`). Stop,
  rebind and teardown finalize without any dialog.
- **Samples are drained on every tick,** including ticks where `latest()` is
  None (link failing), so nothing queued before a dropout is lost.
- **A write failure (disk full, folder removed) stops the recording visibly**
  and keeps the rows already written, instead of raising out of the paint
  timer while the button still says "Recording".
- **The GT link recovers from unplug/replug.** After 3 failed polls the poll
  thread re-opens the GT with backoff (immediately, then 1, 2, 4 ... 10 s),
  re-running VID/PID autodetect so a GT that comes back on a different COM
  number is found. The status line says "still recording; N samples so far
  are on disk" during the outage. Pressing Connect while connected no longer
  tries a second open of the same port (it skips the backoff instead).
- **A silent GT fails a poll in about 1.2 s, not about 30 s.** No `>` prompt on
  a PID request raises `GtNoResponse` straight away. An all-empty poll (not
  even ATRV) now counts as a failure instead of resetting the failure count.
- **A failed first poll releases the COM port** (it used to stay held, so
  every later Connect failed with "Access is denied").
- **Serial open sets `write_timeout`**, so a dead Bluetooth SPP link cannot
  block a write forever. "Access is denied" now names the likely holder.
  `stop()` closes the port under the IO lock and does not hang if a poll is
  stuck.
- **Columns cover every channel the link can produce** (`recordable_channels`,
  which uses the ECM's supported-PID bitmap), not just the ones that answered
  the first poll. `tft` (trans fluid temp) and `run_time` had no column and
  were silently dropped.
- `j2534.find_dll` only returns a DLL whose PE bitness matches the process.
  The OBDX installer registers both 64-bit and 32-bit DLLs.
- `tests/smoke_gui.py` records through the real `toggle_record` and uses a fake
  replay clock, so the replay cannot loop on a slow machine. This is the same
  approach as #33, which it supersedes.

### Fixed (truck-mcp sessions)
- Record on a bound truck-mcp session said "use truck-mcp session_export"
  for every session, including archived ones, so a user trying to log a
  drive was sent to export data that was not the truck. The refusal now says
  whether the bound session is live or archived (and SIMULATED, when it is),
  and always says how to record the truck: press "🔌 Connect GT Pro", then
  Record. The session_export hint is kept only for a live session. Recording
  a truck-mcp session stays refused.

### Added
- `tmstore.list_sessions()` reports `simulated` per session, read from the
  session's own provenance (`sweep_config.started_by == "seed_demo_data"` or
  notes beginning "SIMULATED", as truck-mcp's seed_demo_data.py writes them).
  The Dashboard and Live Data session pickers tag those `[SIMULATED]`, and
  the status line after binding one says SIMULATED.
- GT live path: freshness is now tracked per channel. A PID missing from one
  poll round shows its last real reading marked "carried" (before, it blanked
  to "—" and the needle dropped to zero for that round). A link that stops
  producing new rounds shows "not advancing". The stale threshold adapts to
  the poll rate (3x the interval, kept between 2 and 10 s). The GT's own
  "read failed" still overrides everything. truck-mcp and replay sources are
  unchanged.
- A green "live" mark on every gauge whose value is fresh, in the same place
  the other freshness badges appear.
- Dashboard keyboard shortcuts, each listed in its button's tooltip: Ctrl+G
  connect GT, Ctrl+T truck session, Ctrl+R record, F5 start, Shift+F5 stop,
  P pause/resume replay.

### Release tooling
- `_deploy.ps1` backs up the outgoing exe, verifies the installed version
  resource, and creates/repoints the Start menu and Desktop `OpenOBD`
  shortcuts at the installed exe.
- Release path is now in the repo: `_build.bat` is committed (runs from any
  checkout via its own `.venv`, and fails if the built exe's version resource
  differs from `__version__`), and `docs/RELEASE.md` covers test, build,
  version check, deploy with `_deploy.ps1`, post-deploy check and rollback.

### Changed
- Dashboard gauges move fluently: needles, arcs and bar fills ease toward
  each new reading at display rate (~60 fps, time-based, no overshoot),
  independent of the 1-10 Hz sample rate. The readout still prints the true
  latest sample; the easing only moves the geometry.
- Gauge readouts use fixed decimals per scale (whole units for rpm, speed and
  temperatures; tenths for MAF, kPa, percent, volts, trims, knock retard), so
  the text no longer changes width between samples.
- Gauges repaint only when their value, state or eased position changes. Each
  dial's and bar's face, ticks, numerals and scale are drawn once into a
  cached pixmap rather than redrawn every 100 ms tick.
- The bar strip (classic) and the ring strip (modern) flow into a balanced
  grid that reflows on resize. Gauges for channels the bound source can't feed
  are hidden. The RPM/Speed anchor dials stay in place, dimmed.
- The Dashboard status line wraps, so the Dashboard no longer forces a minimum
  window width of about 1150 px.
- Gauge widgets and cluster layouts moved from `app.py` to `openobd/gauges.py`.
  The pure easing, freshness and grid logic is in `openobd/gaugemotion.py`.
- Added `.pre-commit-tests` so the dev-standards pre-commit hook runs the pytest
  suite and refuses a commit on red; it previously printed "TESTS DID NOT RUN".

### Fixed (window width)
- The main window could not be made narrower than about 1526 px once a drive
  session was bound in Live Data. A tab widget is as wide as its widest page,
  and the Live Data top bar put a session picker sized to its longest label
  and a status line that never wrapped on one row (1522 px). The picker now
  reserves a fixed character budget, grows into spare room, and elides when
  narrow. Its tooltip and its dropdown, which is widened to fit, still show
  the full session label. The status line wraps. Live Data now needs 936 px
  (744 px with no session). The Diagnostics Module Map status line had the
  same problem (1080 px) and now wraps too (464 px). A regression test holds
  Live Data to 1000 px. The window is still held at about 1238 px by the
  Dashboard source/status row and the Tuning editor toolbars (in `app.py`),
  which are tracked separately.

## 0.31.0 — 2026-10-05

### Fixed
- The importer took a `7F AE 78` "response pending" as the module's answer.
  It is "busy, wait", not a refusal: the importer now keeps looking for the
  final answer (the BCM's nine `AE00` hand-backs were all accepted after a
  pending), and reports "pending-only" if none arrives in the capture.
- A control seen several times kept only its first answer. Catalog entries
  now tally every observed answer (`observed_counts`); the list shows them.

### Added
- Catalog: 26 controls imported from the Autel session of 2026-10-05
  15:46-15:56 (canrec-main-20261005-154648.jsonl): transfer case control 01
  (bits 40/80/02 on/off, motor drive down/up), transmission controls 30/38
  (data not yet decoded), BCM controls 10/12/13 (bit on/off) and the BCM
  hand-back `AE00`. Named by Autel time; TCCM/BCM entries read as mask+value.

## 0.30.1 — 2026-10-05

### Changed
- `_run_canrec.bat` asks only for the bus (M / B). The recording is named
  automatically, `canrec-main-YYYYMMDD-HHMMSS.jsonl` or
  `canrec-body-YYYYMMDD-HHMMSS.jsonl` (local time), so it matches the
  function run on the other tool by when it was made. `canrec` still accepts
  an explicit label.

## 0.30.0 — 2026-10-05

### Added
- **Body-bus capture and control.** `canrec --bus sw` (and `_run_canrec.bat`,
  which now asks M = main / B = body) records the single-wire GMLAN body bus
  (33.3 kbps, pin 1) with filters on the body modules' request / UUDT / USDT
  ids and 0x101. One bus per recording: the GT has refused a second
  simultaneous channel.
- Every recorded frame and catalog entry carries its `bus`. 0x243 is the
  EBCM on the main bus and a body module on the body bus; the importer pairs
  requests and answers within a bus only, and a replay goes back onto the bus
  it was recorded from. Body-bus entries are id'd `SW-...`.
- `devctl --run` for a body-bus control reads the engine/speed interlocks on
  the main bus (the ECM is not on the body bus), then moves the SAME device
  session to the body bus to actuate, keep alive and return control ($20).
  Vehicle speed cannot be re-read during a body-bus run; the plan says so
  and the result records `motion_monitoring: false`.

### Tests
- 427 pass; the wrong-bus mutant (skip the switch to the body bus) is killed.
  Not yet run on the truck.

## 0.29.1 — 2026-10-05

### Fixed
- `_run_devctl.bat`'s control list was one unlabelled line, so it did not read
  as a list and the prompt did not say what to type. The list now has a
  heading, numbered entries with the module named in words, the module's last
  answer in plain English ("module refused it (device control limits
  exceeded)"), and the prompt asks for the NUMBER; `--run` accepts the number
  or the ID.

## 0.29.0 — 2026-10-05

### Added
- **Gated device control** (`openobd/devctl.py`, `_run_devctl.bat`), Ron-run.
  GM modules cannot list their device controls and probing one actuates it,
  so controls come only from captures: `--import CANREC_JSONL` extracts every
  `$AE` exchange another tool (the Autel) made, with the module's answer, into
  `openobd/data/controls.json` (status "captured"). Seeded with the Autel's
  TCCM learn, `7E4 AE0302020000` (observed answer `7F AE E3 00 0A`).
- `--run ID` replays the entry BYTE FOR BYTE after: engine running (ECM PID
  0C) and vehicle stopped (PID 0D) read live; Neutral attested by the operator;
  an exact typed phrase at an interactive terminal (refused without a TTY).
  During the run: `$3E` every 1 s, speed re-read every 1 s (motion aborts),
  TCCM position pair streamed (`$2C/$AA`). On EVERY exit it sends `$20`
  ReturnToNormalMode and stops the stream. Per-run wire allowlist; every frame
  logged to devctl-<id>-<time>.json.
- 13 tests; checked mutants (no `$20` on exit, no confirmation, no interlocks)
  all killed. Not yet run on the truck.

### Limits (stated, not hidden)
- Single-frame requests only (the captured TCCM control is 6 bytes).
- Transmission range is operator-attested, not measured.
- "Everything available" grows by capture: run each Autel active test /
  special function with `_run_canrec.bat` recording, then `--import` it.

## 0.28.0 — 2026-10-05

### Added
- `openobd/tccmprobe.py`: precise, NON-ACTUATING interrogation of the TCCM
  (7E4/7EC, UUDT 0x5EC) over one raw-CAN pass-thru channel.
  - `--failure-records` (`_run_tccm_failrec.bat`): GMLAN `$12 01` lists the
    failure records the module froze when it set a code; `$12 02` reads each
    back with its parameter bytes -- the module's own record of why C0398 set.
  - `--stream LABEL` (`_run_tccm_stream.bat`): `$2C` packs identifiers into
    DPIDs and `$AA` streams them on 0x5EC, the way the Autel streamed this
    module. Position A and B (22:3114/3115) share ONE DPID, so they are
    sampled at the same instant (didscan --watch read them ~33 ms apart).
    Fast rate, falling back to medium on a NAK; `$AA 00` stop on every exit.
- Allowlist enforced before the wire: `$12` (sub 01/02 only), `$1A`, `$22`,
  `$2C`, `$AA`, `$3E`. Device control, clears, security access, sessions and
  programming are refused in code. Own ISO-TP: single-frame requests, flow
  control for a multi-frame reply. 29 tests; two hand mutants (flow control
  dropped, `$AE` allowed) both killed. Not yet run on the truck.

## 0.27.0 — 2026-10-04

### Added
- `openobd/canrec.py` + `_run_canrec.bat`: LISTEN-ONLY recorder of HS-GMLAN
  diagnostic traffic through the GT's pass-thru driver, for capturing what
  another scan tool sends (the Autel on the Y-splitter running the TCCM Range
  Actuator Learn). Raw CAN 500k with pass filters on the diagnostic id ranges
  only (7E0-7EF, 7DF, 101, 24x, 54x, 5Ex, 64x); every frame with a timestamp
  to canrec-<label>.jsonl. No transmit path (pinned by a test).

## 0.26.0 — 2026-10-04

### Added
- `didscan --watch LABEL`: read-only live recording (~5 samples/s, 25 s) of the
  TCCM identifiers that moved with the knob, to a CSV. `_run_tccm_watch.bat`.
  Purpose: during an AUTO -> 4HI attempt, tell "the TCCM never drives the
  motor" from "the motor turns but the mechanism/second sensor does not
  follow" (C0398).

### Truck finding (2026-10-04, 2HI/AUTO/4HI/4LO snapshots)
- 22:3142 = knob request (2HI 02, AUTO 05, 4HI 04, 4LO 03): the TCCM sees
  the selector correctly.
- 22:3114 / 22:3115 = actuator position pair (2HI 375/374, AUTO 625/623):
  the two readings agree to 1-2 counts at rest. The actuator moved 2HI ->
  AUTO and then stayed at 625 for the 4HI and 4LO requests.

## 0.25.1 — 2026-10-04

### Fixed
- `_run_tccm_read.bat` needed the position label as an argument, so a
  double-click printed usage and closed instantly; three truck reads silently
  did nothing. It now asks for the knob position when started without one and
  pauses so the result can be read. The discover script also pauses at the end
  and says up front that its window stays quiet while it works.

## 0.25.0 — 2026-10-04

### Added
- `openobd/didscan.py`: READ-ONLY data-identifier discovery for one module
  over pass-thru ISO 15765 (default the TCCM, 7E4/7EC). `--discover` asks
  every `$1A` ident and every `$22` DID in a range and saves each answer as it
  arrives (resumable with `--start`); `--read LABEL` re-reads the supported set
  for one vehicle state; `--diff` lists identifiers that change between
  states. The sender refuses every service but `$22`/`$1A`, so it cannot
  clear, write or actuate. One device session per run.
- `_run_tccm_discover.bat` and `_run_tccm_read.bat LABEL` for the truck:
  locate the transfer case shift-position readings (what Tech2Win's data
  display would show) by reading in 2HI / AUTO / 4HI and diffing.

## 0.24.1 — 2026-10-04

### Fixed
- **Text-mode `$A9` reads were cut short** (the BCM gave 46 of its 66 table
  entries over ELM, all 66 over pass-thru). An ELM-style adapter stops
  listening and prints its prompt once no frame has arrived for its timeout
  (~200 ms, adaptive); a module pausing longer mid-table lost the rest. The
  per-module read and the functional sweep now set ATAT0 + ATSTFF (fixed ~1 s
  wait) for the capture, restore ATST32 + ATAT1 after, and wait up to 6 s.
- **Class fix: a cut table can no longer pass as a short one.** Every GM `$A9`
  table ends with a 00 00 end-of-table marker (verified: one per table in the
  2026-08-06 truck capture, BCM after 76 reports, EBCM after 45).
  `parse_a9_report` returns `complete`; a read without the marker is reported
  "INCOMPLETE" in dtcscan (per-module lines and both sweeps) and in
  dtcscan.json, with the data it did get.

## 0.24.0 — 2026-10-04

### Added
- **TCCM identified: 7E4/7EC on HS-GMLAN, DTCs via GM `$A9` on UUDT 0x5EC.**
  The pass-thru functional sweep (17:20) got a transfer-case DTC table from
  0x5EC (C0306 C0321 C0374 C0379 C0387 C0392 C0396 C0397 C0398 ...), the
  "unidentified 7EC responder" since August. It never answered the 24x
  chassis addressing. The module map now places the TCCM on HS (7E4/7EC), the
  per-module scan reads it with `$A9` using its real report/NAK ids
  (`read_gmlan_dtcs(uudt_id=, usdt_id=)`, `vehnet.GMLAN_ROUTED`), and the
  topology ping includes it.
- Sweep output names the powertrain-block UUDTs: 0x5E8 ECM, 0x5EA TCM,
  0x5EB FPCM, 0x5EC TCCM.

### Truck finding
- TCCM C0398 status DB (current, lamp commanded): Range Position Correlation
  -- the shift motor's incremental position sensor and the rotational position
  sensor disagree by 5% or more. This is the Service 4WD message.

## 0.23.3 — 2026-10-04

### Changed
- First successful SW-GMLAN body-bus sweep on the truck (17:17): 11 modules
  answered (0x542-0x55D) and 0x641 refused. To IDENTIFY them, the sweep now
  prints every responder's full supported-DTC table (its fingerprint: C03xx =
  transfer case, C07xx = tire pressure) and writes every raw (code, symptom,
  status) record to `dtcscan.json`, rewritten per bus so a driver crash cannot
  lose it.
- Fault codes print once each with only their fault statuses (healthy
  duplicate records of the same code were printed too); status bit1 (currently
  failed) is marked "current".

## 0.23.2 — 2026-10-04

### Fixed
- **The OBDX pass-thru driver killed the scan process.** First truck run of the
  binary-mode path: the driver (a .NET DLL) threw an unhandled
  `InvalidOperationException: The port is closed` from its background serial
  thread after a device close, terminating Python, and the buffered output
  died with it. Now: ONE device session per run (`swcan.sweep_buses`), each
  bus on its own channel, every result handed to the caller before the single
  close; dtcscan prints each result as it lands, line-buffered even when
  redirected to dtcscan.out.

### Known
- The driver's close race itself is in the vendor DLL. If it still fires at
  the final close, every result has already been printed.

## 0.23.1 — 2026-10-04

### Fixed
- A GT left in pass-thru (binary) mode no longer aborts the scan. The first
  0.23.0 run left the GT in binary mode, and the next run stopped at the
  text-mode connect even though binary mode is exactly what the pass-thru
  sweeps need. dtcscan now catches GtBinaryMode, skips only the text-mode
  reads, and runs BOTH `$A9` sweeps through pass-thru (HS-GMLAN on plain CAN
  500k, then SW-GMLAN). `swcan.sweep(bus="hs"|"sw")`.

## 0.23.0 — 2026-10-04

### Added
- **Single-wire GMLAN body bus (SW-CAN, 33.3 kbps, pin 1) through the GT's
  pass-thru driver** (`openobd/swcan.py`). The GT's ELM327 text mode has no
  single-wire protocol and rejects the STN extensions, but its pass-thru driver
  registers SW_CAN_PS and SW_ISO15765_PS. The new sweep sends only `$A9 81`
  (read DTCs): functional to AllNodes 0x101, then physical 0x241-0x25F for ids
  not yet heard, and lists every responder with status-filtered fault codes.
  This is where the transfer case module is expected to be.
- Pass-thru client: `pass_filter()`, `J1962_PINS` pin selection (SW-CAN = pin
  1), SW-CAN baud constant.
- dtcscan runs the SW sweep LAST, after the serial port is released, prints
  the OBD-port battery voltage, and warns that the GT is left in binary mode
  (unplug USB + OBD for 10 s before the next scan).

### Not yet proven on hardware
- The SW sweep is tested against a fake pass-thru client only. The first truck
  run is its bring-up.

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
