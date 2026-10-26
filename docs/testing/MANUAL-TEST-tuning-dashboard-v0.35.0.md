# OpenOBD v0.34.0 / v0.35.0 - Manual Test Script: Dashboard redesign and Tuning tab UX overhaul

**Covers**: v0.34.0 Dashboard controls redesign (PR #52), v0.34.1 release-tooling fix (deploy gate; checked in X-12), v0.35.0 Tuning tab UX overhaul (PR #54)
**Sources of truth**: `CHANGELOG.md` sections 0.34.0, 0.34.1, 0.35.0; `openobd/tuningui.py`; `openobd/app.py` (Tuning editor toolbar, menus); `openobd/gauges.py`; `tests/test_dashboard_controls.py`; `tests/test_tuningui.py`
**Components**: `openobd/app.py` (Dashboard, Tuning page), `openobd/tuningui.py`, `openobd/gauges.py`, `openobd/theme.py`, `openobd/editops.py`, `openobd/model.py`
**Target hardware**: 2010 Silverado, OBDLink MX+ (truck-mcp) and OBDX Pro GT (USB 0483:5740)
**Author**: test-script-scribe  **Date**: 2026-10-26

## 1. Purpose

Hand-verify what the automated suite cannot: how the redesigned Dashboard and Tuning workspace look and feel on real screens, and that the Dashboard behaves correctly against a real OBDX Pro GT on the bench. Each case has a pass/fail box; a run is complete when every P0 case is checked Pass.

The automated tests (`python -m pytest tests/ -q`, `tests/smoke_gui.py`) must be green first. This script does not repeat their assertions; it exercises the same behaviours through the real UI.

## 2. Preconditions and test data

| Item | Requirement |
|---|---|
| Build under test | `python -m openobd` from the `feat-tuning-ux` worktree, or the deployed exe. Help -> About must read `OpenOBD 0.35.0`. Record the version here: `________` |
| Screens | Run the whole script at **1366x768** and again at **1920x1080** (window maximised, Windows scaling 100%). Cases marked [BOTH] must be checked at both sizes. Cases marked [1366] only matter at the smaller size. |
| Calibration A (sample) | `data/2010_silverado_stock.cal.json` (File -> Open .cal.json, Ctrl+O). Every table has a stock baseline, so a fresh load shows no changed dots. |
| Calibration B | `data/2010_silverado_best.cal.json`. Used for the "open another .cal" case (X-04) and to see pre-changed tables. |
| Table with no stock baseline | One table whose `stock_values` is null. If none exists in A or B, copy A to `D:\Scratch\<date>-openobd-nostock\nostock.cal.json`, delete the `stock_values` key of one table in an editor, and open that copy. Note the table name: `________` |
| Test table T | A 2D table with at least 8x8 cells and a stock baseline, e.g. "Open Loop IVT Gain vs. IVT vs. MAP" (HPT id 12441). Note any other 2D table you use as T2: `________` |
| Sample log | Any GT recording CSV with rpm, vss, app, spark, knock_retard (e.g. a `gt_log_*.csv` from Dashboard recording, or the fixture `tests/smoke_gui.py` writes). Needs at least one knock event for L-05. |
| Simulated source | A truck-mcp session whose provenance is simulated (File -> Open truck-mcp session... lists it with the `[SIM] ` prefix). Used wherever "simulated" is stated. |
| GT bench rig | OBDX Pro GT on USB, powered from a bench supply or the truck in accessory/ignition-on (engine off is fine). Driver installed; the COM port appears in Device Manager under USB ID 0483:5740. Cases marked **[GT]** need it; all others run with no hardware. |
| Excel | Microsoft Excel (or LibreOffice Calc) for the TSV round trip (E-17). |
| Clean state | Before the first run delete the app's QSettings (Recent list, overlay panel, dashboard style) or note the current values. Cases N-04, E-26 and D-03 depend on this. |
| Cleanup | After the run: discard unsaved edits (Edit -> Undo until the title loses its `*`), delete recordings made during D-12/D-13 and any scratch copies, reset Dashboard visual to the original value. Never save over the files in `data/`. |

Do not run any case against the truck while it is moving or the engine is under load. Recording and Reconnect cases are bench-only.

---

## 3. Dashboard (v0.34.0)

### D-01 Modern cluster style [BOTH] - P0
1. Settings -> Preferences... (Ctrl+,). In "Dashboard visual" choose "Modern cluster - dual dial with tiles". Click OK. Expected: status bar shows "Dashboard visual: Modern cluster - dual dial with tiles" for about 4 s; the Dashboard switches immediately without restart.
2. Open the simulated session (File -> Open truck-mcp session..., pick a `[SIM] ` entry). Expected: dual dial plus tile layout; every gauge reads its value; no overlapping text or clipped numerals at either screen size.
3. Resize the window from 1366x768 down to the minimum and up to 1920x1080. Expected: tiles and dials reflow; no gauge becomes unreadable; no horizontal scrollbar.

Pass: [ ]  Fail: [ ]  Notes: ____

### D-02 Classic style [BOTH] - P0
1. Settings -> Preferences..., choose "HP Tuners classic - dial pair + bar cluster", OK. Expected: status bar "Dashboard visual: HP Tuners classic - dial pair + bar cluster".
2. With the simulated session bound, check the layout. Expected: six dials (MAF, MAP, RPM, Speed, ECT, IAT) with RPM and Speed large; RPM shows yellow from 5500 and red from 6200; ECT yellow from 215 and red from 235; IAT yellow from 120 and red from 150; bar strip below (Advance, TPS, Pedal, Load, STFT, LTFT, ...). The dial faces match the reference `3-recording-1280x800.png`.
3. Bars with limits (STFT/LTFT at +/-10, Fuel below 40 psi, Volts below 12, Cat above 1600) draw the limit mark in red.

Pass: [ ]  Fail: [ ]  Notes: ____

### D-03 Style persists across restart - P1
1. Set the style to the one that is NOT current. Quit (File -> Quit) and relaunch.
2. Expected: the Dashboard opens in the style chosen, and the Preferences dialog pre-selects it.
3. Restore the original style.

Pass: [ ]  Fail: [ ]

### D-04 Gauge state: held/carried value is badged, not shown as live - P1
Setup: a truck-mcp session or replay that produces carried values (any session where a channel is read less often than it is displayed).
1. Watch a channel whose value is carried between reads. Expected: while the value is fresh the gauge shows the green "live" mark; once carried it shows the amber caption "carried" in the same slot.
2. Expected: a carried value never looks identical to a live needle (the readout colour takes the amber accent).

Pass: [ ]  Fail: [ ]

### D-05 Gauge state: stale ("not advancing") - P1
1. Bind a live truck-mcp follow session and let the writer stop (or stop the truck-mcp session mid-follow). After the staleness limit (5 s for truck-mcp sessions) expected: the gauges show the amber caption "not advancing" and keep the last value; they do not keep claiming "live".
2. Restart the data. Expected: the caption returns to "live" within a few samples.

Pass: [ ]  Fail: [ ]

### D-06 Gauge state: failed + GT poll health - P0 **[GT]**
1. Connect the GT (auto-connect, D-18) with ignition on. Expected: gauges show live values.
2. Pull the OBD connector from the vehicle side of the GT (USB stays attached) or switch ignition off so polls fail. Expected: within a few poll cycles the affected gauges show the red caption "read failed" and the status line gives the failure reason (stable text, not flickering).
3. Restore the connector/ignition. Expected: the "read failed" caption clears and the green "live" mark returns on every gauge that reads again; no restart needed.

Pass: [ ]  Fail: [ ]  Notes: ____

### D-07 Unavailable channels collapse; anchors stay dimmed - P1
1. With a source that cannot feed some channels (the GT path, or a log with few columns), look at the bar strip. Expected: bars for channels the source cannot supply collapse out of the way and the remaining bars re-balance across the width.
2. Bind a source with no RPM (a log without rpm). Expected: the big RPM and Speed dials are NOT hidden; they stay in place, dimmed (grey face and numerals, no needle value).

Pass: [ ]  Fail: [ ]

### D-08 Transmission temperature is dimmed on the GT path - P1 **[GT]**
1. Connect the GT. Locate the "Trans" bar (unit F, range 80-300).
2. Expected: the Trans gauge is dimmed (grey, no value, no needle fill) and never shows a constant such as 195.8 F. Start a recording (D-12) and open the CSV: it has NO "Trans Fluid Temp (F)" column.

Pass: [ ]  Fail: [ ]

### D-09 Transmission temperature still fed by replay/truck-mcp sources - P2
1. Load a HP Tuners or truck-mcp log that has a Trans Fluid Temp column and replay it.
2. Expected: the Trans gauge is active (not dimmed), shows the logged value, and turns red above 240 F.

Pass: [ ]  Fail: [ ]

### D-10 R toggles recording; Ctrl+R still works [GT] - P0
1. With a live GT and focus anywhere on the Dashboard press R. Expected: recording starts, the button turns to the STOP state (D-11). Press R again. Expected: recording stops and finalizes; the status line keeps the saved path.
2. Repeat with Ctrl+R. Expected: identical behaviour.
3. With no live GT (idle, or replay) press R. Expected: nothing starts; the status line or the button tooltip explains why.

Pass: [ ]  Fail: [ ]

### D-11 Record button label, state and disabled reasons [BOTH] - P0
For each state, read the button text, enabled state and tooltip (hover 1 s):

| State | Expected button | Tooltip contains |
|---|---|---|
| Nothing bound | disabled | a reason that recording needs a live GT |
| GT live, idle [GT] | red, enabled, text starts "RECORD" with the record glyph | - |
| Recording [GT] | text `STOP  .  <n> samples  .  mm:ss` e.g. "1,234 samples  .  02:14" (reference screenshot) | - |
| GT stopped/disconnected | disabled | a reason |
| Replay of a log | disabled | "Recording needs a live GT; this is a replay" |
| truck-mcp session (incl. [SIM]) | disabled | text telling you to plug in the GT or choose "Settings -> Reconnect GT now" |

Also check: the button is on its own row under the status line, is 56 px tall (measure in a screenshot: 56 +/- 1 px), and spans the width at both resolutions.

Pass: [ ]  Fail: [ ]  Notes: ____

### D-12 Recording elapsed counter and sample count [GT] - P1
1. Start a recording and watch for 30 s. Expected: elapsed runs mm:ss counting up from the first recorded sample (not from the click), the sample count increases monotonically, and thousands show a separator (1,234).
2. Click STOP. Expected: recording finalizes; Log Analysis is offered; the offered file opens in Log Analysis when accepted.

Pass: [ ]  Fail: [ ]

### D-13 Record, no samples lost at stop [GT] - P0
1. Start recording, wait 20 s, stop through each path in turn on separate runs: (a) STOP button, (b) Settings -> Disconnect GT, (c) File -> Open truck-mcp session... binding another source, (d) closing the app window.
2. Open each CSV. Expected: for every run the last row timestamp is within one poll cycle of the stop moment, the file is complete (not truncated, not zero-length), and the sample count shown on STOP matches the CSV row count (+/- 1).
3. For run (d): the recording is finalized before the window closes (no orphaned partial file, the COM port is released: Device Manager shows no "in use", and relaunching auto-connects).

Pass: [ ]  Fail: [ ]  Notes: ____

### D-14 Mark button and M key [GT] - P0
1. While idle (not recording). Expected: the MARK button is not shown; pressing M does nothing harmful (no error).
2. Start a recording. Expected: MARK appears beside STOP, same height (56 px), same row.
3. Press M three times, and click MARK twice. Expected: five markers are written; the CSV/metadata contains five marks at plausible times; a held M key does not write a flood of marks (writes are coalesced).
4. Stop. Expected: MARK disappears.

Pass: [ ]  Fail: [ ]

### D-15 Replay strip, Space and P pause [BOTH] - P0
1. Load a log (File -> Load Log..., Ctrl+L). Expected: a replay strip shows Start, Stop, Pause, Speed and the position slider.
2. Press Start, then Space. Expected: replay pauses (button shows paused state); Space again resumes. Repeat with P.
3. Hover the Pause button. Expected: tooltip includes "(Space)".
4. Drag the slider. Expected: gauges jump to that position.
5. Bind a truck-mcp session. Expected: the strip shows Start/Stop only (no Pause/Speed/slider, session is not seekable).
6. Bind the live GT [GT]. Expected: NO Stop button in the strip; F5 and Shift+F5 do nothing to the live GT.

Pass: [ ]  Fail: [ ]

### D-16 Shortcut list in the status-line tooltip - P2
1. Hover the Dashboard status line. Expected: tooltip lists R (record), M (mark), Space (pause on a replay), plus the existing F5/Shift+F5 and Ctrl+R / P.

Pass: [ ]  Fail: [ ]

### D-17 Menu changes: no Connect/Session buttons on the Dashboard - P1
1. Expected: the "Connect GT Pro" button and the session picker button are gone from the Dashboard.
2. Settings menu contains "Reconnect GT now" (Ctrl+G) and "Disconnect GT". File menu contains "Open truck-mcp session..." (Ctrl+T).

Pass: [ ]  Fail: [ ]

### D-18 GT auto-connect when plugged in [GT] - P0
1. Start OpenOBD with the GT unplugged. Expected: status line "Looking for OBDX Pro GT... (plug in the USB)" and nothing else; the app stays responsive.
2. Plug in the GT. Expected: within about 2 s it connects with no click; status line leaves the "Looking" text and gauges go live; no second connect attempt while it is live.
3. Start OpenOBD with the GT already plugged in. Expected: connects at startup (the scan runs when the Dashboard is built), not after a manual action.

Pass: [ ]  Fail: [ ]

### D-19 GT busy/held port: backoff and one stable status line [GT] - P1
1. Open the GT's COM port in another program (a serial terminal) so OpenOBD cannot open it, then plug in or restart.
2. Expected: one status line "OBDX Pro GT found on COMx but it could not be opened (<reason>) . retrying". It stays one line (no flicker, no stacking). Retries come after about 5 s, then 15 s, then every 30 s.
3. Close the other program. Expected: the next retry connects. Use Settings -> Reconnect GT now (Ctrl+G) during a 30 s wait. Expected: it retries immediately, skipping the backoff.
4. Unplug and replug. Expected: the backoff resets to 5 s.

Pass: [ ]  Fail: [ ]

### D-20 Disconnect GT and Reconnect GT now [GT] - P0
1. With a live GT choose Settings -> Disconnect GT. Expected: gauges go to the disconnected state; Record disabled; if a recording was active it is finalized and its saved path stays in the status line.
2. Leave the GT plugged in for 20 s. Expected: it is NOT re-opened.
3. Press Ctrl+G. Expected: it connects. (Or unplug and replug: also lifts the suppression.)

Pass: [ ]  Fail: [ ]

### D-21 GT does not hijack a bound log or session [GT] - P1
1. Replay a log (or bind a [SIM] session) with the GT plugged in. Expected: no auto-connect; status line gains "- OBDX Pro GT detected - Settings -> Reconnect GT now to switch".
2. Press Ctrl+G. Expected: the GT binds and the replay ends.
3. Start a connect, then immediately bind a log. Expected: the log stays bound; the late GT connect result is released, not bound.

Pass: [ ]  Fail: [ ]

### D-22 truck-mcp session picker with [SIM] prefix - P1
1. File -> Open truck-mcp session... (Ctrl+T) from the Tuning tab. Expected: the app switches to the Dashboard and a menu opens under the File menu.
2. Expected: a disabled header "Stored sessions - not the truck; recording needs a live GT"; simulated sessions prefixed `[SIM] `; with no sessions the entry "No drive-log sessions found" appears under the header.
3. Pick a session. Expected: it binds; Record is disabled (D-11).

Pass: [ ]  Fail: [ ]

---

## 4. Tuning Navigator (v0.35.0)

Open Calibration A. Click the Tuning tab.

### N-01 Search as you type and Ctrl+F / Esc [BOTH] - P0
1. On the Dashboard press Ctrl+F. Expected: nothing (shortcut only active on Tuning).
2. Go to Tuning, click in the table grid, press Ctrl+F. Expected: focus moves to the "Search tables (Ctrl+F)" field.
3. Type `spark`. Expected: the tree filters on every keystroke; matching tables only.
4. Press Esc. Expected: the field clears and the full tree returns.
5. Search for a word that appears only in a table's note text, upper case. Expected: that table is found (note search, case-insensitive).
6. Search for nonsense. Expected: tree empty; no categories shown.

Pass: [ ]  Fail: [ ]

### N-02 Categories, collapse and counts [BOTH] - P0
1. Expected: each category row reads "<Category> (<n>)" and the n equals the number of table rows beneath it (count manually for one small category).
2. Collapse and expand a category by click and by Left/Right arrows.
3. Type a filter. Expected: counts shrink to the matching tables only; a category with no matches is hidden entirely.
4. Clear the filter. Expected: original counts return.

Pass: [ ]  Fail: [ ]

### N-03 Changed dot, no-stock ring, blank [BOTH] - P0
1. On a fresh load of A: no table shows a dot (all blank).
2. Edit one cell of T (E-05). Expected: a filled dot appears on T's row immediately. Undo the edit. Expected: the dot disappears.
3. Open the no-stock table. Expected: a hollow, muted ring (neither dot nor blank); tooltip contains "No stock baseline". Edit a cell in it: the icon stays a ring, not a dot.
4. In Calibration B, expected: tables that differ from stock show dots on first display.
5. Check at both screen sizes in the dark palette: dot (accent), ring (muted) and blank are distinguishable without relying on colour alone (shape differs).

Pass: [ ]  Fail: [ ]

### N-04 Recent group persists across restart - P0
1. Open 10 different tables one after another by clicking them. Expected: a "Recent (8)" group appears at the top; it holds the 8 most recent, newest first.
2. Re-open the 5th entry. Expected: it moves to the top; the count stays 8.
3. Click a Recent entry. Expected: the table opens and the group re-orders AFTER the click; no crash, no flicker of the clicked row.
4. Type a search. Expected: Recent is hidden while a search term is present; clearing shows it again.
5. Quit and relaunch, open Calibration A. Expected: the same 8 tables in the same order.
6. Open a calibration that lacks one of those tables. Expected: the missing table drops from Recent silently.

Pass: [ ]  Fail: [ ]

### N-05 Keyboard navigation - P1
1. Ctrl+F, type `table`, press Down. Expected: focus/selection moves into the tree's first table row (not a category).
2. Press Down/Up repeatedly. Expected: selection moves row by row; Enter opens the selected table in the Editor and switches to Tuning -> Editor.
3. With the filter text "idle" in the search field, press Enter. Expected: the first match opens.
4. Press Esc from the search field. Expected: it clears.

Pass: [ ]  Fail: [ ]

### N-06 Breadcrumb header and chips - P0
1. Open T. Expected: header reads "<Category> > <Table name>" in bold, with chips at the right: unit (if any), "HPT id <n>", and "<k> cells changed" (neutral when 0, red/danger when above 0), plus the table note muted underneath.
2. Edit 3 cells. Expected: chip reads "3 cells changed" (singular "1 cell changed" for one) and updates on every edit/undo.
3. Open the no-stock table. Expected: a muted chip "no stock baseline" and NO "cells changed" chip.
4. A long table name at 1366x768. Expected: the header elides with an ellipsis rather than widening the window; hover shows the full text.

Pass: [ ]  Fail: [ ]

---

## 5. Tuning Editor (v0.35.0)

Open Calibration A and table T. Select a 3x3 block of cells unless stated. Amount = 1.0 unless stated. Edit -> Undo (Ctrl+Z) / Redo (Ctrl+Y) after each case to restore.

### E-01 One 36 px toolbar, everything present [BOTH] - P0
1. Expected: exactly one toolbar under the breadcrumb, 36 px high (measure), in this order: Amount spin box (default 1.0000), then = + - x / % icon buttons, separator, Interp (H), Interp (V), Interp 2D, Smooth, separator, "Revert sel -> stock", "Next D", Copy, Paste, separator, Heatmap (checkable, on by default), "Diff vs stock" drop-down.
2. Hover every button. Expected: a tooltip describing it and its key where one exists (Next D "(F3)", Copy "(Ctrl+C)", Paste "(Ctrl+V)", math keys "=", "+", "-", "*", "/", "Shift+5").
3. At 1366x768 some buttons may move to the toolbar overflow (">>") menu. Expected: overflowed actions still work from that menu and the window does not get wider.

Pass: [ ]  Fail: [ ]

### E-02 Amount field - P0
1. Amount accepts values from -1,000,000,000 to 1,000,000,000 with 4 decimals. Type 0.5, -2.25, 12.3456, and 1000000000.
2. Expected: the value is kept as typed (4 decimals); letters are rejected.
3. Expected: the math in E-03 uses the current Amount.

Pass: [ ]  Fail: [ ]

### E-03 Math actions and grid keys: = + - x / % [BOTH] - P0
Using a single cell with value v, Amount a. Test each by toolbar button AND by grid key (click the grid first):

| Action | Button / key | Expected new value |
|---|---|---|
| Set | = / `=` | a |
| Add | + / `+` | v + a |
| Subtract | - / `-` | v - a |
| Multiply | x / `*` | v * a |
| Divide | / / `/` | v / a |
| Percent | % / `%` (Shift+5) | v scaled by a percent (a = 10 gives v * 1.10) |

1. For each row perform the action. Expected: the value changes as stated; the status bar/undo text names the action; ONE undo step per action (Edit menu shows "Undo add" etc.).
2. Select a 3x3 block and apply +. Expected: all nine cells change, one undo step restores all nine.
3. Divide with Amount 0. Expected: refused, no cell changes, no crash; message in the status bar.

Pass: [ ]  Fail: [ ]

### E-04 Auto-repeat keys do not flood undo - P1
1. Select one cell. Hold `+` for 3 seconds.
2. Expected: exactly one undo step is created (value changed by Amount once), not one per repeat. Edit -> Undo once restores the original.

Pass: [ ]  Fail: [ ]

### E-05 In-cell edit - P1
1. Double-click a cell, type `1.234`, Enter. Expected: value updated; changed triangle appears (E-21); chip "1 cell changed"; navigator dot appears.
2. Type `abc`: refused, value unchanged. Type `inf`, `-inf`, `nan`, `1e999`: all refused with a status message; value unchanged and no undo step added.

Pass: [ ]  Fail: [ ]

### E-06 Interpolate horizontal / vertical / 2D - P0
1. Select a row segment of at least 4 cells; set the first and last to distinct values. Click "Interp (H)". Expected: the interior cells become a linear ramp between the end cells; end cells unchanged.
2. Select a column segment of at least 4 cells with distinct ends; "Interp (V)". Expected: linear ramp down the column.
3. Select a 4x4 block with distinct corners; "Interp 2D". Expected: a smooth bilinear fill between the four corners.
4. Each is ONE undo step; undo restores exactly.

Pass: [ ]  Fail: [ ]

### E-07 Smooth - P0
1. Set a 3x3 block to values 0..8 and the centre to 100 (via =). Select only the centre cell, click Smooth. Expected: centre becomes the mean of itself and its 8 neighbours (sum / 9); neighbours unchanged.
2. Select a corner cell (top-left of the table). Expected: the mean uses only the neighbours that exist inside the table (4 cells), not wrap-around or zero padding.
3. Select a cell in a 1-row table or a table edge row. Expected: same inside-table rule.
4. Undo restores; Redo re-applies; undo again restores.

Pass: [ ]  Fail: [ ]

### E-08 Revert selection to stock - P0
1. Edit five cells, select two of them, click "Revert sel -> stock" (also Edit -> "Revert selection -> stock"). Expected: those two return to the stock values; the other three keep their edit; chip drops by two; their corner triangles disappear.
2. Undo. Expected: the two edits return. (Revert goes through the undo stack.)
3. Edit -> "Revert whole table -> stock". Expected: all cells return to stock; chip reads "0 cells changed"; navigator dot clears.

Pass: [ ]  Fail: [ ]

### E-09 Next change (F3) - P1
1. Edit three cells far apart. Click a cell above/left of them. Press F3 repeatedly.
2. Expected: selection jumps to each changed cell in reading order, then wraps or reports none further; the same via the toolbar button "Next D".
3. On an unchanged table press F3. Expected: status bar says there is nothing changed (no crash, selection unchanged).

Pass: [ ]  Fail: [ ]

### E-10 Copy as TSV - P0
1. Select 2x2 cells, press Ctrl+C. Paste into Notepad. Expected: two lines, two tab-separated values each, values full precision, no headers.
2. Expected: Ctrl+C on a single cell yields one value, no trailing tab.

Pass: [ ]  Fail: [ ]

### E-11 Paste TSV: one undo step - P0
1. Copy a 2x2 block. Select a different single cell, press Ctrl+V. Expected: 2x2 block pasted with the top-left at the current cell; ONE undo step named "paste"; Ctrl+Z restores all four.

Pass: [ ]  Fail: [ ]

### E-12 Paste clipped to the grid - P1
1. Put "1<TAB>2<TAB>3 / 4<TAB>5<TAB>6" (two lines) in the clipboard (type in Notepad, select, Ctrl+C). Select the bottom-right cell of the table and press Ctrl+V.
2. Expected: only the top-left value `1` lands (in the bottom-right cell); nothing outside the table changes; the status bar mentions "clipped"; undo restores.

Pass: [ ]  Fail: [ ]

### E-13 Paste refuses non-numeric content - P0
1. Clipboard "RPM<TAB>800 / 1<TAB>2" (two lines). Paste. Expected: the WHOLE paste is refused; no cell changes; no undo step added; status bar contains "rejected" and says why.
2. Clipboard "1<TAB>x". Expected: same (a single bad field rejects everything).
3. Clipboard with a blank cell among numbers in a rectangular range: see E-15.

Pass: [ ]  Fail: [ ]

### E-14 Paste refuses nan / inf - P0
1. Clipboard `nan`, then `inf`, then `-inf`, then `1e999`. Paste each into a selected cell.
2. Expected for each: refused, status bar explains, cell unchanged, no undo step.
3. Clipboard "1<TAB>2 / 3<TAB>inf" (two lines): the entire 2x2 is refused, including the good numbers.

Pass: [ ]  Fail: [ ]

### E-15 Paste keeps interior blank rows - P1
1. In Excel, enter 1 in A1, leave A2 empty, enter 3 in A3. Select A1:A3, Ctrl+C. Paste in OpenOBD at row r.
2. Expected: row r gets 1, row r+1 is left blank/unchanged per the app's blank handling, row r+2 gets 3. The value 3 must NOT shift up into row r+1.

Pass: [ ]  Fail: [ ]

### E-16 Copy/Paste only act in the Editor - P0
1. Copy a cell value in the Editor. Switch to Tuning -> Scalars and press Ctrl+V. Expected: nothing is written to the (hidden) calibration table; undo stack unchanged.
2. Repeat on Log Analysis, Dashboard, Diagnostics, Live Data. Expected: same.
3. Check Edit menu: Copy and Paste are greyed out away from the Editor and enabled on it.
4. Return to the Editor. Expected: Ctrl+V works again.

Pass: [ ]  Fail: [ ]

### E-17 Excel round trip - P1
1. Copy a 4x4 block from OpenOBD, paste into Excel. Expected: 16 numeric cells, no text, correct order.
2. Edit a few values in Excel, copy the range, paste into the same position in OpenOBD. Expected: values land exactly; changed chip counts the differences from stock; one undo step.
3. Copy a range including an Excel header row (text). Expected: refused as in E-13.

Pass: [ ]  Fail: [ ]

### E-18 Overflow refusal "edit refused: non-finite result" - P0
1. Select one cell with a non-zero value. Set Amount to 1000000000 (1e9). Click x repeatedly; press it about 40 times.
2. Expected: at some press the status bar reads "edit refused: non-finite result"; the cell keeps its last finite value, never shows inf/nan, and no undo step is pushed for the refused press.
3. Undo repeatedly. Expected: returns cleanly to the original value.
4. Repeat with Smooth, Interp and Revert after forcing huge values. Expected: none can create inf/nan.

Pass: [ ]  Fail: [ ]

### E-19 Heatmap on/off [BOTH] - P0
1. Heatmap on (default). Expected: blue-cyan-green-yellow-red scale from the table's minimum (blue) to its maximum (red); axis headers neutral; cell text readable on every colour (light text on dark cells, dark text on light cells).
2. Toggle Heatmap off. Expected: cells use the plain dark palette background; text still readable.
3. Toggle on again. Expected: identical colours as before.
4. Edit the max cell. Expected: scale re-stretches to the new min..max.

Pass: [ ]  Fail: [ ]

### E-20 Diff vs stock: Off / Absolute / Percent [BOTH] - P0
1. Edit three cells (+5, +10%, one back to stock value). Open the "Diff vs stock" drop-down. Expected: three checkable entries Off / Absolute / Percent; the button caption reads "Diff vs stock", "Diff: Absolute" or "Diff: Percent".
2. Absolute. Expected: each cell shows its delta (value minus stock) in text; scale diverges blue (negative) / grey (zero) / red (positive); hover shows the real value in the tooltip.
3. Percent. Expected: deltas as percentages; same colour scheme.
4. Off. Expected: back to values.
5. Open the no-stock table with Diff Absolute. Expected: every cell reads "n/a" (not 0).
6. The chosen mode persists while switching tables within the session.

Pass: [ ]  Fail: [ ]

### E-21 Changed-cell corner triangle - P1
1. Edit a cell. Expected: a small orange corner triangle appears at the cell's top-right.
2. Expected: it is visible in heatmap, plain, Absolute and Percent modes; it disappears when the cell is reverted or undone.

Pass: [ ]  Fail: [ ]

### E-22 Axis headers and corner - P1
1. Expected: column headers show the X breakpoints with unit (e.g. "-40 F"); row headers show Y breakpoints with unit (e.g. "20 kPa"); the corner reads "Y \ X" style axis names (e.g. "kPa \ F" in the reference screenshot).
2. Click the corner. Expected: it still selects the whole table (the label does not eat the click).
3. Rows/columns that contain changed cells or the selection are shown bold (as in the reference screenshot, bold 25-35 kPa rows and 11.2-32 F columns).

Pass: [ ]  Fail: [ ]

### E-23 Hover tooltip - P1
1. Hover a changed cell for 1 s. Expected: tooltip shows X, Y, value, stock and delta with percent.
2. Hover an unchanged cell. Expected: delta 0 / 0%.
3. Hover a cell in the no-stock table. Expected: no stock/delta numbers invented; tooltip says no stock baseline.

Pass: [ ]  Fail: [ ]

### E-24 Selection readout strip [BOTH] - P0
1. No selection: strip reads "No selection". Select 4 cells: "4 cells . min .. . max .. . mean .. . D vs stock n changed", with the unit after each number; singular "1 cell".
2. Change two cells of the selection. Expected: "2 changed" updates immediately.
3. Select cells in the no-stock table. Expected: the Delta text is replaced by "no stock baseline" (never "0 changed").
4. Select the whole table at 1366x768. Expected: the strip elides with an ellipsis instead of widening the window.

Pass: [ ]  Fail: [ ]

### E-25 Dirty flag and undo/redo returning clean - P0
1. After opening Calibration A (no edits): window title has no unsaved marker.
2. Make an edit. Expected: title shows the unsaved marker (`*`).
3. Undo back to the loaded state. Expected: the marker disappears (dirty flag clears when the undo stack returns to its clean state).
4. Redo. Expected: marker returns. Edit, then Ctrl+S, then edit again then undo once: Expected: returns to the SAVED state and the marker clears again.
5. Make an edit, undo, make a different edit. Expected: marker stays set.

Pass: [ ]  Fail: [ ]

### E-26 Overlay side panel and its persistence - P1
1. Right of the grid is a "Log overlay" disclosure button. Expected: collapsed by default on a clean profile; only the button takes room.
2. Click to expand. Expected: panel shows Mode (None / Log count / Log mean value), X channel, Y channel, Value, and the help text.
3. Load the sample log (File -> Load Log...), choose Log count and select the table's X and Y channels. Expected: cells tint by hit count; "Log mean value" with a Value channel tints by mean. Switching tables rebuilds the tint without lag (16x16 table redraws smoothly).
4. Leave the panel expanded; quit; relaunch. Expected: panel opens expanded. Collapse, quit, relaunch: collapsed.

Pass: [ ]  Fail: [ ]

### E-27 Undo/redo menu text and stack - P2
1. Perform: set, add, smooth, paste, revert. Edit menu shows the correct "Undo <action>" for each in reverse order. Ctrl+Z five times then Ctrl+Y five times returns to the exact same values.

Pass: [ ]  Fail: [ ]

---

## 6. Scalars (v0.35.0)

### S-01 Grouping and category filter [BOTH] - P0
1. Open Tuning -> Scalars. Expected: parameters grouped under collapsible categories; columns Parameter / Value / Unit / Stock / D.
2. Toolbar "Category" combo lists "All categories" plus each category. Choose one. Expected: only that group shows; the breadcrumb path ends with the category name.
3. Choose "All categories". Expected: all groups return.

Pass: [ ]  Fail: [ ]

### S-02 Search - P1
1. Type a known parameter name in "Search parameters". Expected: it filters as you type; matching parameters remain under their category; categories with none are hidden.
2. Type nonsense. Expected: empty list. Clear (the field's clear button). Expected: all return.

Pass: [ ]  Fail: [ ]

### S-03 Validation - P0
1. Double-click a Value cell, type `abc`, Enter. Expected: refused, value unchanged, status message.
2. Repeat with `inf`, `nan`, `1e999`. Expected: refused each time, no undo step, original value shown again.
3. Type a valid number. Expected: accepted.

Pass: [ ]  Fail: [ ]

### S-04 Delta chip and undo - P0
1. Change a scalar by +1. Expected: the D column shows a rounded red chip "+1"; the unchanged scalars show no chip.
2. Edit -> Undo. Expected: chip disappears and the value is restored; Redo restores the chip. The undo text is "edit <parameter name>".
3. "Revert to stock" toolbar button with the row selected. Expected: returns to stock and the chip clears; undoable.

Pass: [ ]  Fail: [ ]

### S-05 Breadcrumb and no-stock scalar - P2
1. Select a scalar. Expected: breadcrumb shows a "<Category> > <Name>" style path with chips.
2. For a scalar with no stock value (if any): chip says "no stock baseline", never "0 changed".

Pass: [ ]  Fail: [ ]

### S-06 Safety banner - P2
1. Expected: the calibration's safety note shows as a muted banner at the bottom of Scalars, wraps at 1366x768 and does not widen the window.

Pass: [ ]  Fail: [ ]

---

## 7. Log Analysis (v0.35.0)

### L-01 Empty state - P0
1. With no log loaded open Tuning -> Log Analysis. Expected: an empty-state message (not blank plot axes); breadcrumb ends with "No log loaded"; toolbar shows Load Log, Events and "Learn pedal calibration".

Pass: [ ]  Fail: [ ]

### L-02 Load a log; header chips [BOTH] - P0
1. Click Load Log and choose the sample log. Expected: breadcrumb "Log Analysis > <file name>" with chips such as "400 samples", "40 s", "5 channels"; the plot area replaces the empty state.

Pass: [ ]  Fail: [ ]

### L-03 Channel tree groups - P0
1. Expected: a checkable tree of channels grouped Engine / Fuel / Spark / Trans / Sensors / Other (only groups with channels appear, each "Name (n)").
2. Uncheck a group's box. Expected: all its channels uncheck and their plots disappear. Check it again. Expected: they come back. A partly checked group shows the partial (tri-state) box.
3. Check one channel only. Expected: exactly one plot strip is added.

Pass: [ ]  Fail: [ ]

### L-04 Cursor readout - P0
1. Move the mouse over the plots. Expected: the right-hand "Cursor / Value" table shows a first row `t` as "<seconds> s" and a row per plotted channel with its value at the cursor (e.g. t 22.00 s, rpm 4400).
2. Move off the plot. Expected: no error; values reset to a dash or hold the last position consistently.
3. Toggle channels. Expected: rows follow the plotted set.

Pass: [ ]  Fail: [ ]

### L-05 Events toggle - P1
1. Events is on by default. Expected: dashed vertical lines mark events (red for knock).
2. Toggle Events off. Expected: lines disappear; on again: they return.

Pass: [ ]  Fail: [ ]

### L-06 Analysis text pane - P2
1. Expected: the text pane under the plots shows Source, Samples, Duration, Channels and the Regime trims / Knock sections. A pedal-uncalibrated log says so rather than reporting a WOT result.

Pass: [ ]  Fail: [ ]

---

## 7A. Diagnostics (v0.36.0)

**Covers**: v0.36.0 Diagnostics tab UX overhaul (PR #55). Sources: `CHANGELOG.md` section 0.36.0, `openobd/diagui.py` (`DiagnosticsPage`, `ModuleMapView`, `ModuleCard`, `LegendRow`), `tests/test_diagui.py`. IDs are `G-nn`. Cases marked [BOTH] are checked at 1366x768 and 1920x1080, [1366] only at 1366x768, [GT] need the OBDX Pro GT on the truck.

**Preconditions addendum (Diagnostics)**
- GT connected to the truck (USB plugged in, OBD cable on the DLC), ignition ON, engine off; the Dashboard may be live or idle (both must work, see G-02).
- A truck with at least one known stored code is preferred (note it: `________`). Without one, G-15 and G-19 to G-22 can only check the empty and "No trouble codes" paths; mark the code-dependent steps N/A with a reason.
- Cases that clear codes (G-23 to G-26) run on a bench or test truck only. Clearing resets every readiness monitor and needs a full drive cycle to recover. Never run them on a truck that must pass an emissions check. Hardware-dependent cases that cannot be provoked on the bench (marked "if reproducible") are recorded N/A with the reason; their logic is covered by `tests/test_diagui.py`.

Open Diagnostics from the main tab bar. The two sub-tabs are "Module Map" (left) and "Codes & Readiness" (right).

### 7A.1 Module Map

### G-01 Empty state before any scan [BOTH] - P0
Setup: fresh app launch, no scan yet this session.
1. Open Diagnostics -> Module Map. Expected: breadcrumb "Diagnostics > Module Map" in bold; one chip at the right reading "not scanned" (muted); the note under the title starts "Not scanned" and says Scan Modules (F5) uses the live GT link or opens its own.
2. Look at the right-hand card. Expected: the empty-state text "Not scanned yet. Scan Modules (F5) checks the USB link, DLC power, the bus and every module, using the live GT link or opening its own." No module details, no "Scan notes" panel.
3. Look at the map. Expected: every module node reads "not scanned" in the grey "not scanned" colour; the HS-GMLAN modules show their request/response ids, and the SW-GMLAN modules (IPC, SDM, HVAC, Radio) do NOT read "unreachable" before a scan; no failed-segment marker anywhere.
4. Look at the toolbar. Expected: "Scan Modules" enabled (highlighted as the primary button); "Copy report" DISABLED.
5. Click Scan Modules once. Expected: after the scan completes the SW-GMLAN modules read "unreachable" (amber, dashed rail) and only now.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-02 Scan Modules busy chip and busy state always restored [BOTH] - P0 **[GT]**
1. Click Scan Modules. Expected: immediately a muted "Scanning..." chip is the LAST chip in the header; Scan Modules, Read Codes and Clear Codes are all disabled (one GT job at a time); the window stays responsive.
2. While scanning press F5 and click the disabled buttons. Expected: nothing starts a second job.
3. Wait for the scan to finish. Expected: the "Scanning..." chip disappears; Scan Modules, Read Codes and Clear Codes are enabled again; the header chips and the "Scanned at hh:mm:ss" note are updated.
4. Repeat the scan with the Dashboard live on the GT, and again with the Dashboard idle. Expected: both complete; with the Dashboard live its polling pauses during the scan and resumes afterwards.
5. Failure path (if reproducible): unplug the USB cable while a scan runs. Expected: the scan ends with the error banner of G-10, and the buttons are re-enabled; the busy state is restored even when the result handler fails (it must never stay stuck disabled or show "Scanning..." forever). Check by pressing F5 again: a new scan starts.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-03 Header chips after a scan [BOTH] - P0 **[GT]**
1. After a successful scan expected chips, left to right: "DLC <v> V" (neutral, e.g. "DLC 12.6 V"), "<n> responding" (green when n > 0), "<k> not examined" (muted; only present when k > 0), "fault at: <segment or module>" (red; only present when a fault was found).
2. Expected: the note reads "Scanned at hh:mm:ss" with the time of this scan, plus the hover/click hint.
3. With every module answering, expected: no "fault at" chip and no failed marker on the map.
4. Resize the window to its minimum width. Expected: the chips shrink or clip; they do not widen the window.
5. Where the DLC voltage cannot be read (if reproducible), expected: the first chip reads "DLC not read" (muted), never a made-up voltage.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-04 Fault chip and failed-segment marker - P1 **[GT]**
1. Produce a fault: switch the ignition OFF (or unplug the OBD cable from the DLC) and scan. Expected: a red "fault at: <label>" chip, for example "fault at: DLC power", "fault at: USB link (PC to GT)" or a module name such as "fault at: EBCM (ABS)".
2. Expected: the failed segment or module is drawn red/orange, and the first failed segment carries a vector warning triangle with an exclamation mark (a drawn triangle, not an emoji or missing-glyph box).
3. Restore ignition/cable and rescan. Expected: the fault chip and marker are gone.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-05 Legend row [BOTH] - P1
1. Expected: a row directly under the toolbar (above the map) with five swatch + label pairs in this order: responding, no response, unreachable via this path, not examined, not scanned. Swatch colours match the node colours of those states (green, red, amber, cool grey, grey).
2. The map no longer paints its own legend (no duplicate legend inside the map area).
3. Narrow the window. Expected: the legend clips; it does not force the page wider.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-06 Hover tooltips - P1
1. After a scan hover the EBCM node for 1 s. Expected: a tooltip with five lines: the module name, "Status: <status>", "Bus: <bus>", "Diagnostic address: request <req> / response <resp>" (e.g. request 243 / response 643), and "Role: <role>".
2. Hover an SW-GMLAN module (IPC). Expected: the address line reads "none on HS-CAN (single-wire bus only)".
3. Move the pointer over a module and off it. Expected: the cursor is a pointing hand over a node and an arrow elsewhere; the tooltip hides when the pointer leaves the node.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-07 Click a module: module card [BOTH] - P0
1. Scan, then click the EBCM node. Expected: the node gets a thicker, lighter outline; the card at the right shows the title "EBCM (ABS)" with a status chip (here "no response", red), and the rows Bus "HS-GMLAN (500k)", Role "Brake control ...", Diagnostic address "request 243 / response 643".
2. Click an ECM node. Expected: the card updates to ECM with the chip "responding" (green).
3. Click an SW-GMLAN module. Expected: the Diagnostic address row reads "No HS-CAN diagnostic address - reachable only via the single-wire bus." and the status chip is amber ("unreachable").
4. Select a module, then scan again. Expected: the same module stays selected and its card shows the status from the NEW scan.
5. Before the first scan (or after the app restarts) with no module selected, expected after a scan: the card shows the hint "Click a module on the map for its status, bus and address."
6. Select text in the card with the mouse. Expected: the values are selectable.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-08 Scan notes collapsible and its persistence - P1
1. After a scan the "Scan notes" disclosure appears under the card, expanded by default, with one bullet ("• ...") per note (e.g. the EBCM no-answer note, the BCM "not examined" note, the SW-GMLAN note in the reference screenshot).
2. Collapse it, then expand it. Expected: it toggles; the notes are plain text (no formatting of device replies).
3. Collapse it, quit and relaunch, scan again. Expected: it starts collapsed; expand, relaunch, scan: it starts expanded.
4. A scan with no notes. Expected: the panel says "No notes from this scan."

Pass: [ ]  Fail: [ ]

### G-09 Copy report - P1
1. Before any scan the button is disabled (G-01). After a scan click Copy report and paste into Notepad.
2. Expected first line "OpenOBD module scan at hh:mm:ss"; then "DLC: <v> V" (or "not read"); "Fault at: <label>" (or "none found"); a blank line; a tab-separated table headed "module / bus / request/response / status" with one row per module (e.g. "EBCM (ABS)", "HS-GMLAN (500k)", "243/643", "no response"; "-" for modules with no HS-CAN address); then "Notes:" with one "- " line per scan note.
3. After a failed scan (G-10) the pasted report includes the line "Scan failed at the tool link: <reason>".

Pass: [ ]  Fail: [ ]  Notes: ____

### G-10 Inline error banner when the GT is absent [BOTH] - P0
1. Unplug the GT from USB and make sure no other program holds the port. Click Scan Modules. Expected: NO popup dialog; a red banner above the legend reads "Scan failed at the tool link: <reason>"; a red chip "fault at: USB link (PC to GT)" appears; the buttons are enabled again and "Scanning..." is gone.
2. Plug the GT in and scan again. Expected: the banner disappears on the successful scan.
3. Repeat at 1366x768. Expected: a long reason wraps inside the banner; the page does not widen.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-11 Binary-mode advice text - P1
1. If the GT can be put into binary (J2534) mode on the bench, scan in that state. Expected: the red banner reads exactly "OBDX GT is stuck in binary (J2534) mode - unplug it from USB and the OBD port for 10 s, then rescan." (with the em dash as in the product) and the chip reads "fault at: GT in binary mode".
2. Follow the advice, rescan. Expected: the banner clears.
3. If the mode cannot be provoked, record N/A (covered by `test_scan_error_and_binary_mode_banners`).

Pass: [ ]  Fail: [ ]  N/A: [ ]

### 7A.2 Codes & Readiness

### G-12 Empty state before any read [BOTH] - P0
Setup: fresh launch, Codes & Readiness not yet read.
1. Open the Codes & Readiness sub-tab. Expected: breadcrumb "Diagnostics > Codes & Readiness"; one muted chip "not read"; the note says Read Codes (F5) reads stored, pending and permanent codes and readiness, and that Clear Codes also resets every readiness monitor.
2. Expected: the code tree is empty (no "No trouble codes" row, nothing that looks like a clean result); the readiness panel shows "Readiness not read yet. Read Codes (F5) reads it with the codes." and no summary chip claiming "ready"; no MIL banner.
3. Expected: "Copy codes" is DISABLED; "Read Codes" enabled and highlighted; "Clear Codes..." enabled; the search field shows the placeholder "Search codes (Ctrl+F)".

Pass: [ ]  Fail: [ ]  Notes: ____

### G-13 Read Codes busy chip and busy state restored - P0 **[GT]**
1. Click Read Codes. Expected: a muted "Reading..." chip is the last chip; Read Codes, Clear Codes and Scan Modules are all disabled; F5 does nothing further.
2. When the read finishes, expected: the chip disappears; all three actions are enabled again; the tree and readiness grid are filled.
3. Switch to Module Map while a read runs. Expected: Scan Modules is disabled there too (one job at a time); the "Reading..." state is not lost when switching back.
4. Failure path (if reproducible): unplug the GT while reading. Expected: the buttons are re-enabled and the chip is gone (the busy state is always restored, even when a handler fails); F5 starts a new read.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-14 Header chips after a read [BOTH] - P1 **[GT]**
1. Expected chips: first the MIL chip ("MIL ON" red, "MIL off" green, or "MIL not examined" muted), then one chip per kind in the order Stored, Pending, Permanent: "<Kind> <n>" (red for Stored n > 0, amber for Pending n > 0, muted for Permanent n > 0, green for 0 when fully examined).
2. A kind that could not be examined reads "<Kind> not examined" (muted); a kind with an incomplete module reply reads "<Kind> <n> + incomplete" or "<Kind> incomplete" (muted, never green).
3. Compare with the reference screenshot (MIL ON, Stored 2, Pending 1 + incomplete, Permanent 0).

Pass: [ ]  Fail: [ ]  Notes: ____

### G-15 Grouped tree: Stored / Pending / Permanent with counts [BOTH] - P0 **[GT]**
1. Read Codes. Expected: three group rows in this order, each "<Kind> (<n>)": "Stored (2)", "Pending (1 + incomplete)", "Permanent (0)" in the reference. The count n equals the number of code rows beneath the group (count them).
2. Expected columns: Code | Description | Module | Status. The Status cell is a chip ("Stored" red, "Pending" amber, "Permanent" muted); stored code text is red.
3. Expected: "Stored" is open by default; "Permanent" is collapsed unless it holds a code or an unknown; any group holding a not-examined or incomplete row is open.
4. Collapse and expand a group with the mouse and with Left/Right arrows. Expected: it toggles; group rows span the full width.
5. Expected: when n = 0 for a fully examined kind, the group shows "(0)" and no child row.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-16 Not-examined and incomplete rows are muted italic [BOTH] - P0 **[GT]**
1. Produce an unknown (if reproducible): ignition OFF with the GT linked, or any condition where a module's reply is cut short. Read Codes. Expected: for a kind that could not be read, a row "? | Not examined - <reason or 'no module answered'>" with the status chip "not examined"; for a module with a truncated reply, a row "? | Incomplete from <responder id> - <why>" with the Module column naming the module and the chip "incomplete".
2. Expected: these rows are muted grey AND italic, carry their own glyph icon in the Code column (a half-filled circle for incomplete), use a dashed-outline chip, and cannot be mistaken for a code row (no red/amber code text, code column is "?").
3. Expected: their wording is exactly what the read reported; the group containing them is open and its count reads "(not examined)", "(incomplete)" or "(n + incomplete)".
4. If neither can be provoked, record N/A (covered by `test_not_examined_rows_are_present_and_styled`, `test_incomplete_rows_keep_exact_semantics`).

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### G-17 "No trouble codes" vs "No codes in the examined kinds" - P0 **[GT]**
1. On a truck with no codes where all three kinds were examined, Read Codes. Expected: a first row "No trouble codes" with a green "clear" chip.
2. With at least one kind not examined or incomplete and no codes found (G-16 setup), Read Codes. Expected: the row reads "No codes in the examined kinds" with a muted chip, NEVER "No trouble codes".
3. When all three kinds were not examined, expected: no summary row at all (only the three not-examined rows) and no green anywhere.
4. When any code is present, expected: no summary row.

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### G-18 Module column and "No local description" fallback - P1 **[GT]**
1. Expected: the Module column shows the reporting module by name (e.g. "ECM (E38)", "TCM (T43)") derived from the responder id; a responder id that matches no known module shows the raw id; a code with no reporting module shows "-" in muted colour.
2. For a code not in the local description table, expected the Description reads "No local description" in muted colour (never blank).
3. If the truck has no such code, record N/A for step 2.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-19 Search filters codes; Esc clears [BOTH] - P0
1. Read Codes with several rows. Click the search field and type `P04`. Expected: the tree filters on each keystroke; only rows matching code, description OR module text remain, under their group; all groups are expanded while a search is active (reference screenshot: only P0420 remains).
2. Type a module name (e.g. `TCM`). Expected: rows whose Module column matches remain.
3. Type text in lower and upper case. Expected: case-insensitive.
4. Type nonsense. Expected: empty tree, no crash.
5. With the cursor in the search field press Esc. Expected: the field clears and the full tree returns, with the default open/closed groups restored.
6. Click the field's clear button. Expected: same as Esc.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-20 Copy codes: TSV of the visible rows - P1
1. Read Codes, clear the search, click Copy codes, paste into Notepad. Expected: first line "kind<TAB>code<TAB>description<TAB>module", then one line per visible row (group rows are not included).
2. Type `P04` in the search, click Copy codes, paste. Expected: only the visible (filtered) rows are in the TSV.
3. Paste into Excel. Expected: four columns, no merged cells.
4. Not-examined and incomplete rows, when present, appear in the TSV with code "?" and their wording (they are not dropped).

Pass: [ ]  Fail: [ ]  Notes: ____

### G-21 Readiness tile grid and "n/m ready" chip [BOTH] - P0 **[GT]**
1. Read Codes. Expected: a "Readiness monitors" panel at the right with a tile per monitor in a two-column grid, each with the monitor name and a chip "Ready" (green) or "Not ready" (amber); a coloured left edge matches the state.
2. Expected: a summary chip at the top of the panel "<done>/<total> ready" (e.g. "8/10 ready"), green only when every monitor is ready, amber otherwise.
3. If the reply carried an error, expected: the panel says "Readiness not examined - <reason>" and the chip reads "readiness not examined" (muted); no tiles, no green.
4. If the ECM reports no supported monitors, expected the text "The ECM reported no supported readiness monitors." and chip "no monitors reported".
5. Narrow the window. Expected: the grid scrolls within its panel; the window does not widen.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-22 MIL banners - P0 **[GT]**
1. MIL ON: Read Codes on a truck with the MIL lit. Expected: a red banner "MIL is ON - <n> code(s)" above the tree and the red chip "MIL ON".
2. MIL off: Expected: no banner and a green chip "MIL off".
3. MIL not examined (readiness reply failed; if reproducible): Expected: a MUTED banner "MIL not examined - <reason>" and the muted chip "MIL not examined"; it must not read "MIL off".
4. Expected: the banner is hidden before the first read.

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### G-23 Clear Codes confirmation text; Cancel changes nothing - P0 **[GT]** (bench truck only)
1. Click "Clear Codes...". Expected: a warning dialog titled "Clear trouble codes" with the text "Clear ALL stored and pending DTCs?" followed by "This also resets every readiness monitor - the truck will need a full drive cycle before it can pass an emissions check." Buttons: Yes and Cancel; Cancel is the default.
2. Press Enter without choosing. Expected: it cancels.
3. Click Cancel. Expected: no GT traffic starts; no "Clearing..." chip; the rows and readiness are unchanged; all actions stay enabled.
4. Check the button caption. Expected: it keeps the trailing ellipsis "Clear Codes..." (it asks before acting).
5. Press F5 and use the keyboard to confirm that F5 never triggers Clear Codes.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-24 Clear Codes success outcome - P0 **[GT]** (bench truck only)
1. Click Clear Codes, then Yes. Expected: a muted "Clearing..." chip; Read Codes, Scan Modules and Clear Codes disabled.
2. On a confirmed clear, expected: NO outcome dialog; the page re-reads and shows fresh results ("Codes read at" time updates); Stored is empty (or only codes that re-set), and the readiness grid shows monitors reset to "Not ready" (the "n/m ready" chip drops).
3. Expected: the busy state is restored and all actions are enabled.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-25 Clear Codes failure outcome dialogs - P1 **[GT]**
1. Provoke "not sent" (if reproducible, e.g. ignition OFF or the OBD cable pulled right after confirming). Expected: a warning dialog titled "Clear codes" starting "The clear was NOT sent - nothing was changed." followed by the reason (or "addressing not confirmed"). After OK the rows from the re-read are shown and the busy state is restored.
2. Provoke "sent, not confirmed" (if reproducible). Expected: a warning dialog titled "Clear codes" starting "The clear was sent but the ECM did not confirm it." with the error and a line "Acknowledged by: <ids or nobody>".
3. These two are the ONLY popup dialogs in the Diagnostics tab besides the confirmation of G-23; link errors are inline (G-28).
4. If neither can be provoked, record N/A (covered by `test_clear_codes_is_confirmed_and_reports_outcome`).

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### G-26 Failed re-read after Clear Codes never shows the pre-clear rows - P0 **[GT]** (bench truck only)
1. Make sure Stored shows at least one code (note it: `________`). Click Clear Codes, Yes, and make the re-read fail (pull the OBD cable from the DLC or switch the ignition OFF right after confirming; if the timing cannot be hit, record N/A).
2. Expected: the old code rows are NOT shown any more; the tree shows not-examined rows for the kinds that could not be re-read (muted italic "Not examined - ..."); the readiness panel and MIL banner read "not examined"; no "No trouble codes" row.
3. Expected: a banner above the tree names the step that failed (the re-read after the clear), so it is clear that the clear itself may have been sent but its result is unknown.
4. Restore the cable/ignition and click Read Codes. Expected: the banner disappears and the true post-clear state is shown.

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### G-27 "Codes read at" note - P1
1. After Read Codes expected the note reads "Codes read at hh:mm:ss - Clear Codes also resets every readiness monitor." with the time of this read.
2. Read again later. Expected: the time updates. After a clear (G-24) it shows the time of the re-read.
3. A read that fails with a link error (G-28) does NOT change the time.

Pass: [ ]  Fail: [ ]

### G-28 Link error is an inline banner, not a popup [BOTH] - P0
1. Unplug the GT (or hold its COM port in another program). Click Read Codes. Expected: NO popup; a red banner above the tree reads "Couldn't reach the truck: <reason>"; the buttons are enabled again and "Reading..." is gone.
2. Do this after a successful read. Expected: the banner also says "the rows below are from the previous read at hh:mm:ss" and names that read time.
3. Restore the GT and Read Codes. Expected: the banner disappears on success.
4. Repeat at 1366x768. Expected: a long reason wraps inside the banner; the page does not widen.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7A.3 Shortcuts

### G-29 F5 runs the primary action per sub-tab - P0 **[GT]**
1. On Module Map press F5. Expected: a scan starts ("Scanning..." chip). After it ends, switch to Codes & Readiness and press F5. Expected: a read starts ("Reading..." chip).
2. Press F5 while a job runs. Expected: nothing (no second job, no queued job).
3. Hover Scan Modules and Read Codes. Expected: the tooltip ends with "(F5)".
4. Leave Diagnostics for the Dashboard and press F5. Expected: it does what it did before (starts a replay when a log is loaded), and does NOT start a scan or a read (G-31).

Pass: [ ]  Fail: [ ]  Notes: ____

### G-30 Ctrl+F only on Codes; Esc clears - P1
1. On Codes & Readiness click in the tree and press Ctrl+F. Expected: the search field takes focus with its text selected.
2. Type `lean`, press Esc. Expected: the field clears (G-19).
3. Switch to Module Map and press Ctrl+F. Expected: nothing (no focus jump, no switch of sub-tab).
4. Press Ctrl+F on the Dashboard. Expected: nothing. On Tuning it still focuses the table search (X-06).

Pass: [ ]  Fail: [ ]

### G-31 Diagnostics keys do not leak into the Dashboard [BOTH] - P0
1. With a live GT on the Dashboard and Diagnostics open, click a row in the code tree (focus on the tree) and press R, M, Space and P. Expected: they behave as list keys (type-ahead, row select/toggle); no recording starts, no marker, no replay pause (extends X-05).
2. On Module Map press R, M, Space. Expected: nothing happens on the Dashboard; the status line and Record button are unchanged.
3. Press F5 on Diagnostics, then switch to the Dashboard. Expected: the Dashboard state is unchanged (no replay started by the Diagnostics F5).

Pass: [ ]  Fail: [ ]  Notes: ____

### 7A.4 Persistence and cross-cutting

### G-32 Splitter positions persist under diag/ - P1
1. On Module Map drag the splitter between map and card to a clearly different position (card wider). On Codes & Readiness drag the splitter between tree and readiness panel (readiness wider).
2. Quit (File -> Quit) and relaunch; open Diagnostics. Expected: both splitters reopen at the dragged positions.
3. Expected: the settings are stored under keys beginning `diag/` (check the QSettings file: `diag/map_split`, `diag/codes_split`, `diag/scan_notes_open`) and the Diagnostics page wrote no other key.
4. Delete the `diag/` keys, relaunch. Expected: the defaults return (map wider than card, tree wider than readiness) without error.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-33 Minimum width at 1366x768 [1366] - P0
1. Launch at 1366x768, maximise, open each Diagnostics sub-tab. Expected: no horizontal scrollbar; the whole window including the status bar fits the screen.
2. Fill the chrome to its widest: read codes with every kind not examined (or error text), start a scan against an absent GT so the error banner and "Scanning..." chip show. Drag the window narrower until it stops. Expected: the Diagnostics page minimum width is under 700 px and the window minimum width is under 1000 px (measure); chips, note, legend and banners clip/wrap rather than widen the window.
3. At the narrowest width expected: the toolbar remains usable (overflow ">>" if needed) and the search field is still reachable.
4. Check at 1920x1080. Expected: the map fills the area; nothing is stretched into an unusable layout.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-34 Dark palette consistent with Tuning [BOTH] - P0
1. Place Diagnostics and Tuning (Editor) side by side in turn. Expected: the same breadcrumb height, the same 36 px toolbar, the same chip look (rounded outline chips), the same greys, no white or light-grey backgrounds, no default-blue selection colour, no light scroll bars (extends X-02).
2. Check the map canvas, the module card, the legend, the tree, the readiness tiles, the banners (red danger, muted), the dashed "not examined" chips and the disclosure button. Expected: all use the dark palette; text contrast at least 4.5:1.
3. Open the warning dialogs of G-23 and G-25. Expected: dark-themed, not jarring white.

Pass: [ ]  Fail: [ ]  Notes: ____

### G-35 Toolbar chrome and icons - P1
1. Expected: both sub-tabs have a single 36 px toolbar directly under the breadcrumb: Module Map holds "Scan Modules", a separator, "Copy report"; Codes holds "Read Codes", "Clear Codes...", a separator, "Copy codes", a separator, the search field.
2. Expected: every button shows a vector glyph icon (no emoji, no missing-glyph boxes); the primary button (Scan Modules / Read Codes) is visibly highlighted compared with the others.
3. Disable states: while a job runs the three job actions are greyed; Copy report and Copy codes follow G-01 and G-12.
4. Hover each button. Expected: a descriptive tooltip.

Pass: [ ]  Fail: [ ]

### G-36 Keyboard-only pass - P2
1. Without the mouse: Tab to the sub-tab bar, switch sub-tabs with the arrow keys, Tab to Read Codes and press Enter/Space, Tab to the tree and move with the arrow keys, Ctrl+F, type, Esc.
2. Expected: every step works and a visible focus indicator shows on the focused control; the module map itself is mouse-only (record any gap as a defect note, not a fail of this case).

Pass: [ ]  Fail: [ ]

---

**Run log addendum (Diagnostics)**: add the 21 P0 cases of section 7A to the P0 pass count (the existing 42 becomes 63) and record G-nn ids for any defects.

---

## 8. Cross-cutting

### X-01 Minimum width under 1000 px [1366] - P0
1. Launch at 1366x768. Select Tuning -> Editor with a table open and the overlay panel EXPANDED.
2. Drag the window narrower until it stops. Expected: the minimum window width is under 1000 px (measure); no horizontal scrollbar on the Tuning page; the toolbar overflows into ">>" instead of forcing width; the long selection readout and breadcrumb elide.
3. Repeat on Scalars and Log Analysis.
4. Maximise at 1366x768. Expected: the whole window including the status bar fits on screen with no part below the taskbar.

Pass: [ ]  Fail: [ ]

### X-02 Dark palette consistency [BOTH] - P0
1. In every area (Dashboard, Tuning Editor, Scalars, Log Analysis, Diagnostics, Live Data, strip chart, Active Tests) check for white or light-grey backgrounds, mismatched greys, default-blue selection colours, or default light scroll bars.
2. Expected: one consistent dark palette; the Dashboard Record/Mark buttons, toolbars (36 px), breadcrumbs, chips and navigator use the same family of greys; text contrast readable (body text at least 4.5:1).
3. Open the Settings dialog, a QMessageBox (About) and a file dialog opened from the app. Expected: no jarring white dialogs.

Pass: [ ]  Fail: [ ]

### X-03 Chrome identical on the three Tuning sub-tabs - P1
1. Compare Editor, Scalars and Log Analysis. Expected: each has one breadcrumb of the same height and one 36 px toolbar directly below it, at the same positions.

Pass: [ ]  Fail: [ ]

### X-04 Opening another .cal with unsaved edits saves the OLD file - P0
1. Open Calibration A as a copy: save it as `D:\Scratch\<date>-openobd-a\a.cal.json` first (File -> Save As...), so the repo data is untouched. Make one edit (note the cell and the new value: `____`).
2. File -> Open .cal.json... and choose Calibration B.
3. Expected: a prompt "The calibration has unsaved edits. Save before closing?" with Save / Discard / Cancel appears BEFORE B loads.
4. Choose Save. Expected: `a.cal.json` is written with the edit; B then loads. Reopen `a.cal.json` and verify the edited cell has the new value. B's file is unmodified.
5. Repeat and choose Cancel. Expected: B does NOT load; A stays open with its edit.
6. Repeat and choose Discard. Expected: B loads; `a.cal.json` is unchanged.
7. Close the window with unsaved edits. Expected: the same prompt appears; Cancel keeps the app open.

Pass: [ ]  Fail: [ ]

### X-05 Shortcuts do not leak into other views - P0
1. Select a row in Diagnostics and Live Data lists (any list view) and press R, M, Space and P. Expected: they act as list keys (type-ahead, row toggle) and do not start recording, mark or pause the Dashboard.
2. With the Tuning Editor grid focused press R, M, Space. Expected: the Dashboard is not affected; no recording starts.

Pass: [ ]  Fail: [ ]

### X-06 Ctrl+F only on Tuning - P2
1. Press Ctrl+F on Dashboard, Diagnostics, Live Data. Expected: no focus jump into the table search; on Tuning it focuses the search field.

Pass: [ ]  Fail: [ ]

### X-07 State persistence across restart - P1
1. Set: overlay panel expanded, style = classic, Recent list populated. Quit and relaunch.
2. Expected: overlay expanded, style classic, Recent restored. Heatmap and diff mode are per session: heatmap on and diff Off after relaunch.

Pass: [ ]  Fail: [ ]

### X-08 Keyboard-only pass - P1
1. Without touching the mouse: Ctrl+F, type, Enter (open table), Tab to the grid, arrow-key selection, `+`, F3, Ctrl+C, Ctrl+V, Ctrl+Z.
2. Expected: every step works; a visible focus indicator is shown on the focused control.

Pass: [ ]  Fail: [ ]

### X-09 Responsiveness - P2
1. Open a table of at least 16x16 cells and toggle Diff and Heatmap quickly; scroll the grid.
2. Expected: no visible lag (under about 0.3 s per toggle) and no CPU pegging with the overlay on.

Pass: [ ]  Fail: [ ]

### X-10 Idle with no GT attached - P1
1. Launch with no GT attached and no sources bound; leave the app idle 2 minutes.
2. Expected: no dialogs, no hangs, CPU idle, status line stays on "Looking for OBDX Pro GT... (plug in the USB)".

Pass: [ ]  Fail: [ ]

### X-11 About and version - P2
1. Help -> About. Expected: "OpenOBD 0.36.0", vehicle and controller info, and the safety text.

Pass: [ ]  Fail: [ ]

### X-12 Release tooling refuses a stale build (v0.34.1) - P2
1. Per `docs/RELEASE.md`, from PowerShell (not Git Bash) run `python -m pytest tests/test_deploy_gate.py -q`. Expected: all pass.
2. In a throwaway dist/install directory (use the script's `-DistExe`, `-InstallDir`, `-BuildLog`, `-PackageDir`, `-NoShortcuts` parameters, never the real install), run `_deploy.ps1` with an exe whose ProductVersion differs from `__version__`. Expected: it refuses before copying or creating the install directory.

Pass: [ ]  Fail: [ ]

---

## 9. Run log

| Field | Value |
|---|---|
| Tester | |
| Date | |
| Build version | |
| Screen sizes run | 1366x768 [ ]   1920x1080 [ ] |
| GT bench run | Yes [ ]   No [ ] |
| P0 pass count | ___ / 63 |
| Defects raised (ids) | |
