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

## 7B. Live Data (v0.37.0)

**Covers**: v0.37.0 Live Data tab UX overhaul (PR #56). Sources: `CHANGELOG.md` section 0.37.0, `openobd/livedata.py` (`LiveDataPage`, `ValueTile`, `TileGroup`), `openobd/stripchart.py` (`StripChart`, `ChannelTable`, `ChartPane`), `tests/test_livedata_ui.py`. IDs are `LD-nn`. Cases marked [BOTH] are checked at 1366x768 and 1920x1080, [1366] only at 1366x768, [GT] need the OBDX Pro GT on the truck (a live drive). Every other case runs against the fixture sessions below (an archived drive, a simulated live drive, or a prepared copy), so the whole section can be executed on the bench with no truck.

**Preconditions addendum (Live Data)**
- Live Data reads truck-mcp session files (`sessions/*.tmsession.db`) read-only and never opens the serial port; nothing in this section needs the GT unless marked [GT]. Point the app at a scratch data root with the `TRUCK_MCP_DATA` environment variable (launch OpenOBD from a shell where it is set) so the fixtures below are the only sessions listed. Prepare all fixture copies in `D:\Scratch\<yyyy-mm-dd>-livedata-fixtures\truck-mcp\sessions\` (never in the repo, never against the original files).
- Fixture sessions (note the file name used for each: `________`):
  - **S-ARCH**: a copy of a real recorded drive that has ended (`session.ended_utc` set), with a VIN, at least 20 channels across Engine, Trans and Sensors (see LD-31), numeric channels with varying values, at least one carried (not fresh) channel, and a session event labelled "wot". The label shown in the picker is the session label.
  - **S-SIM**: a simulated truck-mcp drive log that is still being written (logger running, about 1 sample per second, `session.source = 'simulated'`). Start and stop it from truck-mcp; keep a terminal for it. Used for every live-view case on the bench.
  - **S-STALE**: a copy of a drive recorded earlier whose `ended_utc` is NULL and whose newest sample is older than a few minutes (or S-SIM after its logger process is killed without ending the session).
  - **S-UNK**: a copy of S-ARCH whose `session.source` is set to an unrecognised value, e.g. `UPDATE session SET source='telepathy'` (use `python -I` with `sqlite3` on the COPY).
  - **S-NOEVT**: a copy of S-ARCH with `DROP TABLE event` run on the copy.
  - **S-TEXT**: a copy with every numeric channel removed or re-kinded so only text/enum channels remain (no chartable channel), or a session whose only channels are text.
  - **S-BAD**: a file named `broken.tmsession.db` containing non-SQLite bytes (e.g. 1 KB of text).
- Clean state: before the first case delete the `livedata/` keys from the OpenOBD QSettings file (`livedata/layout`, `livedata/split`, `livedata/span`, `livedata/groups_collapsed`) so persistence cases start from defaults. Cases that change them say so.
- Cases that edit a fixture file (LD-07, LD-13) edit the scratch COPY only. Never edit, lock or delete a file under the real truck-mcp data root.

Open Live Data from the main tab bar. It is one combined page: the chrome (breadcrumb, 36 px toolbar), the tile area (collapsible groups), and below the splitter the graphed-channels table and the Chart vs. Time strip chart.

### 7B.1 Chrome and view states

### LD-01 Live session chrome: breadcrumb, chips, note [BOTH] - P0
Setup: only S-SIM in the data root, logger running.
1. Open Live Data. Expected: breadcrumb reads "Live Data > <session label>" (the product draws a chevron) with the session label bold; there is no separate status line under the toolbar.
2. Read the chips left to right. Expected: first a green "live"; then "SIMULATED" (red) because the source is simulated; then "<n> channels" (neutral, n equals the number of tiles with no filter and no hidden fields; "1 channel" is singular); then "<m> graphed" (neutral, m equals the number of rows in the graphed-channels table). No "held", no "PROVENANCE UNKNOWN".
3. Read the muted note at the right of the breadcrumb. Expected: "<VIN or 'VIN unknown'>  .  source=simulated  .  as of hh:mm:ss"; the time advances every second while the logger runs.
4. Expected: no red banner; Rescan, Fields..., Hold, the Window combo and the filter field are enabled.
5. Narrow the window. Expected: the chips and note shrink or elide; they do not widen the page.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-02 Stale session reads "not advancing", never live [BOTH] - P0
Setup: S-STALE only (or stop the S-SIM logger process abruptly and wait about a minute).
1. Open Live Data and wait one tick. Expected: first chip is amber "not advancing"; NOT green "live".
2. Look at every tile. Expected: each tile with a value uses the not-advancing palette (amber/grey family, the same one in all tiles) and its caption reads "not advancing"; no tile is green.
3. Look at the top-right of the chart. Expected: the state label reads "not advancing" in the same amber.
4. Look at the note. Expected: "as of" shows the time of the last tick (when the file was last read), not the time of the last sample.
5. Restart the S-SIM logger (live, advancing) and Rescan. Expected: the chip returns to "live" within one tick and tiles return to the live palette.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-03 Archived session [BOTH] - P0
Setup: S-ARCH selected.
1. Expected: first chip is muted "archived"; no "live" chip anywhere.
2. Expected: every tile uses the archive palette with the caption "archived drive"; the chart label (top-right) reads "archived drive".
3. Expected: the Hold button is DISABLED (greyed) with a tooltip that still reads as the Hold help text.
4. Expected: the chart opens on the window "All" and its time axis is wall-clock (see LD-21).
5. Expected: the note shows the drive's VIN, `source=<value>` and "as of hh:mm:ss"; the values never move because nothing is advancing.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-04 Provenance chips - P1
1. Select S-SIM. Expected: red "SIMULATED" chip and note `source=simulated`.
2. Select S-UNK. Expected: amber "PROVENANCE UNKNOWN" chip (not "SIMULATED", not silently absent) and the note shows `source=telepathy` (the raw value).
3. Select S-ARCH (a real drive). Expected: neither provenance chip; the other chips (view state, channels, graphed) are still there.
4. Switch between the three several times. Expected: the provenance chip follows the selected session each time; none lingers from the previous one.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-05 Empty states - P1
1. Point `TRUCK_MCP_DATA` at a folder that does not exist and launch. Expected: the tile area is replaced by the shared empty-state text starting "truck-mcp data root not found." and mentioning the `TRUCK_MCP_DATA` variable and Rescan (F5); breadcrumb is just "Live Data" with no chips and the note "No session bound"; Hold and Fields... are disabled.
2. Point it at an existing root whose `sessions/` folder is empty. Expected: the empty state reads "No drive-log sessions found." with the hint to run a truck-mcp session_drive_log.
3. Create the data root / drop in S-ARCH while the app is open and press Rescan. Expected: the empty state disappears and the session binds.
4. Expected: the empty-state text is plain text (a folder or label containing `<b>` characters is shown literally).

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-06 Session that cannot be opened: danger banner, no stale reader - P0
Setup: data root with S-BAD and S-ARCH.
1. Select S-BAD in the picker (or let it be the only session). Expected: a red banner above the tiles beginning "Could not open session:" followed by the reason; no popup dialog.
2. Expected: the tile area shows the empty state "No session bound: the selected session could not be opened. Pick another, or Rescan (F5)."; the breadcrumb is "Live Data" with no chips (it does not still show the previous session's name, chips or note).
3. Expected: no tiles, table rows or chart traces of any earlier session remain on screen (a session that fails to open does not leave its reader bound).
4. Select S-ARCH. Expected: the banner disappears, tiles and chart load normally.
5. Select S-BAD again. Expected: the same banner returns; the app does not crash, freeze or hold the previous session's file open (the file can be copied/renamed in Explorer).

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-07 Read failure: banner after two failed reads, then recovery - P0
Setup: a COPY of S-SIM made live-looking (`ended_utc` NULL) on a removable drive or an SMB share you can disconnect, as the data root. If neither can be arranged, record N/A with the reason (covered by `test_read_failure_banner_after_two_ticks_and_recovery`).
1. Open the session and confirm tiles are live. Disconnect the drive/share (or otherwise make the file unreadable).
2. After the first failed tick (about 1 s). Expected: NO banner yet (one blip is tolerated); tiles unchanged.
3. After the second consecutive failure. Expected: a red banner "Read failed (2x): <reason>" followed by the line "The tiles and chart show the failure, not the last values: nothing below is live until a read succeeds."; the first chip becomes red "read failed".
4. Expected: every tile shows "-" with a caption "read failed: <reason>" in the failed palette (not the last good value, not the live palette); the chart label (top-right) reads "read failed" and no value anywhere is presented as live.
5. Reconnect. Expected: within one or two ticks the banner disappears, the chip returns to "live", tiles show fresh values again.
6. Expected: the failure counter resets after recovery (one new blip does not show the banner).

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### 7B.2 Toolbar

### LD-08 Toolbar: one 36 px bar, session picker, Rescan [BOTH] - P1
1. Expected: a single 36 px toolbar directly under the breadcrumb, left to right: "Session" label and picker, Rescan, Fields..., separator, "Window" label and combo, Hold, separator, the filter field (placeholder "Filter tiles  (Ctrl+F)"). Same height and position as the Tuning and Diagnostics toolbars. Every button shows a vector glyph icon (no emoji, no missing-glyph boxes).
2. Open the picker with several sessions listed. Expected: each entry reads "<tag><label>  .  <started>  .  <n> ch" (plus "[unreadable]" when the file cannot be read); the dropdown popup is wide enough to show the full text of every entry, with no truncation.
3. Select a long entry, then narrow the window. Expected: the closed picker elides its text when narrow instead of widening the page, and hovering it shows the full label as a tooltip.
4. Hover Rescan. Expected: tooltip "Rescan the truck-mcp session folders  (F5)". Add a new fixture file to the sessions folder and click Rescan. Expected: it appears in the picker; the currently selected session stays selected and its tiles are NOT rebuilt (the displayed values keep updating without a flicker or a reset of min/max).
5. Wait 5 s without clicking (automatic rescan). Expected: the same: the selection and the grid are not torn down on every rescan.
6. Narrow until the toolbar cannot fit everything. Expected: the remaining actions go to the ">>" overflow menu; the toolbar never forces the page wider.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-09 Window span: live only, persisted - P1
Setup: clean `livedata/span`; S-SIM and S-ARCH available.
1. Select S-SIM. Expected: the Window combo reads "1 min" and sits in the toolbar (not above the chart).
2. Choose "5 min". Expected: the chart and the Min / Max / Avg columns re-window immediately.
3. Quit (File -> Quit), relaunch, open S-SIM. Expected: the Window combo reopens on "5 min". Check the QSettings file: `livedata/span` = "5 min".
4. Select S-ARCH. Expected: the combo reads "All" (an archived drive always opens on the whole drive), regardless of the saved span.
5. Choose "30 s" while on S-ARCH, then select S-SIM. Expected: the archived view re-windows to 30 s, but S-SIM opens on "5 min" (the saved live span). A span chosen on an archived drive is applied to that view only and is never written to `livedata/span`; check the QSettings file still reads "5 min".
6. Delete `livedata/span`, relaunch. Expected: a live session opens on "1 min" without error.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7B.3 Hold

### LD-10 Hold freezes tiles, table and chart [BOTH] - P0
Setup: S-SIM running, a moving channel (rpm) visible in the table.
1. Note the rpm tile value `________`, the table Value `________`, and the chart's trace end. Click Hold. Expected: the Hold button shows checked; the breadcrumb gets an amber "held" chip next to "live"; the chart label (top-right) reads "held".
2. Expected: every tile that was live now uses the not-advancing palette (not green) and its caption reads "held · not updating".
3. Wait 20 s while the logger keeps recording. Expected: tile values, table values (Value, Min, Max, Avg) and the chart trace do not change; the note's "as of" time does not advance (nothing was read).
4. Expected: tiles that were already stale/carried keep their own caption ("not advancing" / "carried ...") rather than "held"; Hold changes only live tiles.
5. While held open Fields... and hide one field, then show it again. Expected: the layout change applies at once (the tile disappears and reappears), the re-created tiles show their current value but are frozen again (palette not live, caption "held · not updating"); the page stays held.
6. Expected: the chips still include "held" and the "live" chip is unchanged, so the held state is never mistaken for live.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-11 Release catches up on everything recorded meanwhile [BOTH] - P0
1. Continue from LD-10 after at least 30 s of hold (note the time `________`). Click Hold again to release. Expected: the "held" chip disappears, the chart label returns to "live", tiles return to the live palette with caption "live" on the next tick.
2. Expected: the chart trace shows the samples recorded during the hold (a continuous line with no gap from the hold time to now; the time axis end is "0:00" = now); the Min / Max footers and the table Min / Max / Avg include the values from the hold period (a spike you made in the simulator during the hold is in them).
3. Expected: the tile value equals the newest recorded value, not the value at the moment of hold.
4. Hold again and release immediately several times in a row. Expected: no error, no duplicated chart points, no stuck "held" chip.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-12 Archived drives ignore Hold - P0
1. Select S-ARCH. Expected: Hold is disabled (LD-03).
2. Hold S-SIM, then select S-ARCH in the picker. Expected: the hold is released by the switch: the "held" chip is gone, the Hold button is unchecked and disabled, the archived tiles use the archive palette (not the held caption).
3. Select S-SIM again. Expected: it is NOT held (Hold does not carry over between sessions); tiles are live.
4. Press F5 or the Rescan button while S-ARCH is selected. Expected: no error; Hold stays disabled.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-13 A drive that ends while held is released - P0
Setup: S-SIM running and held.
1. While held, end the simulated drive cleanly in truck-mcp (so `ended_utc` is written).
2. Within a few seconds. Expected: Hold releases by itself: the "held" chip disappears, the Hold button unchecks and greys out, and the first chip becomes "archived" (muted); the tiles show the archive palette and caption "archived drive"; the chart label reads "archived drive" and the axis switches to clock time.
3. Expected: the page does not stay frozen with the live palette, and it does not show an error. The values shown are the final recorded values (everything recorded while held is now visible).
4. Repeat without Hold. Expected: the same switch to "archived" within one tick (a drive that ends while bound stops looking live at once).

Pass: [ ]  Fail: [ ]  Notes: ____

### 7B.4 Tiles

### LD-14 Tile anatomy [BOTH] - P1
1. Expected on each tile, top to bottom: the channel name left and its unit RIGHT-aligned on the same row; the big bold value; the small state caption; a muted footer "min <x> . max <y>".
2. Expected: the unit is not repeated inside the value text. A channel without a unit shows an empty right side (no "None").
3. Resize narrower. Expected: a long channel name elides with an ellipsis and the unit stays visible; hover the tile for the tooltip "<name>: drag onto the graphed-channels table to chart it; right-click for options".
4. Expected: the cursor over a tile is the open hand; right-click opens a menu with Hide, Add to / Remove from graph and Move to lane.
5. Expected: tiles in one group line up in uniform columns; a group with a single tile does not stretch across the page. Resize the window wider and narrower: the column count changes with the width (a normal window shows every tile without a horizontal scrollbar).

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-15 Display honesty: stale, carried, archived, held and failed never look live [BOTH] - P0
Setup: S-SIM (live), S-STALE, S-ARCH. The palette meaning must be the same on every tile.
1. On S-SIM: a tile for a channel sampled this tick is green with caption "live".
2. On S-SIM, find a channel whose value is carried (not freshly measured; e.g. a slow DID channel). Expected: caption "carried . <age>s old" (or "carried . age unknown") in the carried palette, NOT the live green; the age grows each tick until a fresh read.
3. On S-STALE: every tile "not advancing" (LD-02). On S-ARCH: every tile "archived drive" (LD-03).
4. Hold S-SIM: tiles "held . not updating" in the not-advancing palette (LD-10).
5. Channel with no data: caption "no data"; a channel never read: "not read" with the value "- not read"; a module error: "module error"; a value the store contract rejects: value "BAD" with the reason text, red.
6. During a read failure (LD-07): "read failed".
7. Expected across all of the above: green is used ONLY for a fresh value on a live, advancing, un-held session. Take a screenshot of one tile in each state and confirm the palettes are distinguishable without reading the caption (colour is backed by the caption text, never the only cue).
8. Open the graphed-channels table: its Value column follows the same rules as the tiles in every state (for example a held or archived value is not shown as a live number).

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-16 Min / max footer: measured (fresh) samples only - P0
Setup: S-SIM live, then S-ARCH.
1. On S-SIM note a moving channel's footer `________`. Let the value rise above the old max (use the simulator or wait). Expected: the max grows, the min never rises; the footer format is "min <x> . max <y>" with thousands separators for values of 1000 or more (e.g. "min 900 . max 2,500").
2. Find a channel whose only samples are carried (not fresh). Expected: its footer reads "min -- . max --" and never takes the carried value as a min or max.
3. On S-ARCH with the Window set to "All": for a channel, compare the tile footer with the Min and Max columns of the graphed-channels table. Expected: they match (both are computed from fresh samples; the footer covers the session from its start).
4. Select another session and back. Expected: the footer is recomputed for the new session (it does not keep the previous session's min/max).
5. Hold and release (LD-10/11). Expected: the footer includes samples recorded during the hold after release.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-17 Collapsible groups, counts, persistence - P1
Setup: clean `livedata/groups_collapsed`.
1. Expected: tiles are in groups in this order (only groups that have members): Engine, Fuel, Spark, Trans, Sensors, Other. Each header reads "<Group>  .  <n>" with n equal to the number of tiles under it.
2. Click the "Trans" header. Expected: its tiles hide, the arrow turns from down to right, the header count stays. Click again: they return.
3. Collapse Trans and Sensors, quit and relaunch, open Live Data. Expected: both reopen collapsed, Engine open. The QSettings key `livedata/groups_collapsed` holds the JSON list of the collapsed names.
4. Expand them all. Expected: the key is removed from the settings file (not left as an empty list).
5. Set `livedata/groups_collapsed` to garbage text and relaunch. Expected: no crash; all groups open.
6. Collapse a group while filtering (LD-18). Expected: collapsing and the filter work independently.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-18 Tile filter is display-only; Ctrl+F focuses, Esc clears [BOTH] - P0
Setup: S-ARCH with at least 20 tiles; note the Fields... menu state `________` and whether the `livedata/layout` key exists (absent on a default layout).
1. On Live Data press Ctrl+F. Expected: the filter field takes focus and any text in it is selected.
2. Type `rp`. Expected: after each keystroke only tiles whose name or group contains the text remain (rpm in the sample), groups with no match disappear completely, and the header of a partly shown group reads "<Group>  .  <shown> of <total>".
3. Type a group name (e.g. `sensors`). Expected: every tile of that group matches. Type upper case. Expected: case-insensitive.
4. Type nonsense. Expected: no tiles, no crash, no error banner; clear the text and everything returns.
5. With the filter active open Fields.... Expected: every channel is still listed and checked as before; the table and chart still show their rows and traces (the filter does not remove anything from the graph).
6. Expected: `livedata/layout` in the settings file is unchanged (still absent on a default layout) and the "<n> channels" chip does not change while filtering.
7. Put the cursor in the filter and press Esc. Expected: the field clears and all tiles return, with the saved group collapse state intact.
8. Click the field's clear button. Expected: the same as Esc.
9. Quit and relaunch with text left in the filter. Expected: the filter is empty on startup (it is not persisted) and all tiles show.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7B.5 Graphed-channels table

### LD-19 Columns, swatch, window statistics - P1
1. Expected columns in order: Field, Value, Unit, Min, Max, Avg, Lane. Hover each header. Expected: a tooltip on every one (Value mentions the live / not advancing / archived / carried rules; Min/Max/Avg mention the visible chart window).
2. Expected: each Field cell has a colour swatch icon (not a "■" character) matching its trace colour on the chart and the colour of the name; rows are compact and uniform.
3. Expected: the Lane column shows a neutral chip "Lane <n>" per row; channels in the same lane show the same number.
4. Set the Window to "All" on S-ARCH, then to "30 s" on S-SIM. Expected: Min, Max and Avg re-compute over the visible window each time (shorter window: min/max narrow; Avg changes), and a channel with no samples in the window shows "-" in all three.
5. Expected: the table height fits its rows (no empty band under a two-row table) up to a maximum, then it scrolls; the chart keeps the remaining room.
6. Right-click a row. Expected: the same channel menu as the tile (Hide, Remove from graph, Move to lane).

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-20 Empty-state row and drop semantics - P1
1. Remove every field from the graph (right-click each row -> Remove from graph (keep tile)). Expected: one row spanning the table with the centered muted text "Drag a tile here to graph it"; the chart shows "No fields on the graph - drag a tile onto the table above."; the "<m> graphed" chip reads "0 graphed".
2. Drag a numeric tile onto that empty row. Expected: a new lane is created and the field is graphed; the empty row is gone; the chip counts up.
3. Drag another numeric tile onto an existing row. Expected: it joins THAT row's lane (same Lane number, the same chart lane).
4. Drag a tile onto the table below the last row (blank area). Expected: it starts a new lane.
5. Drag a text/enum tile (a non-chartable channel) onto the table. Expected: the pointer shows the forbidden cursor and the drop is refused visibly; nothing is added (it is never accepted and silently lost).
6. Use Fields... -> "Reset layout to defaults". Expected: the original lanes return.
7. Customise the layout again, quit and relaunch. Expected: the customised layout is restored (`livedata/layout` key present); after "Reset layout to defaults" the key is removed.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7B.6 Strip chart

### LD-21 Time axis: relative for live, clock for archived [BOTH] - P1
1. S-SIM with Window "1 min". Expected: a time axis under the last lane with the left caption "time (m:ss ago)"; labels count back from the right edge: "0:00" at the right edge, "-0:10", "-0:20" ... (steps adapt to the width; labels never overlap and the edge labels stay inside the plot).
2. Set the Window to "5 min". Expected: the labels re-space ("-1:00", "-2:00" ...); over an hour a label like "-1:00:00" is correct.
3. S-ARCH. Expected: the caption reads "time (HH:MM:SS)" and the ticks are wall-clock times on whole multiples of the step (e.g. 10:00:30, 10:01:00); they do not move when you wait.
4. Switch from S-ARCH to S-SIM and back. Expected: the axis mode follows the session each time.
5. Resize the chart narrower and wider. Expected: the tick count follows the width; no crash with a very narrow chart, and the chart cannot be made narrower than its minimum width.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-22 Hover cursor, readout box and "@ Cursor" [BOTH] - P1
1. Move the mouse over the plot area of the chart. Expected: a vertical hairline follows the pointer and a readout box appears with the first row the time under the cursor (S-SIM: "-m:ss"; S-ARCH: "HH:MM:SS") and one row per graphed channel, "<name>  <value> <unit>", each row in the colour of its trace.
2. At the same time look at the table. Expected: the Value column header reads "@ Cursor" and each row shows that channel's value at the cursor time (the last measured sample at or before that instant); a channel with no sample yet at that time shows "-" (the cursor never invents a value).
3. Move to the right half so the box would leave the plot. Expected: the box flips to the left of the hairline instead of clipping.
4. Leave the chart (mouse out). Expected: the hairline and box vanish, the header returns to "Value", and the table shows the live values again (not the last cursor values).
5. Move the mouse over the label column or the scale column at the right (outside the plot). Expected: no cursor and the header stays "Value".
6. On S-SIM keep the mouse still over the plot for 10 s. Expected: the readout follows the scrolling window (it shows the value at the instant now under the pointer).
7. Hover in a session with no samples yet. Expected: no cursor, no crash.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-23 Session event markers - P1
1. Select S-ARCH, Window "All". Expected: a faint dashed vertical line crosses every lane at the time of the event, with its label (e.g. "wot") at the top of the line.
2. On S-SIM choose a window shorter than the age of an old event. Expected: markers outside the window are not drawn.
3. Hover near a marker. Expected: the marker is a decoration: it does not intercept the cursor, and its label does not cover the state label at the top-right.
4. On S-SIM record a new event while it runs. Expected: it appears within a few seconds and scrolls left with the data.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-24 Session file without an event table: no markers, no error - P0
Setup: S-NOEVT (event table dropped).
1. Select S-NOEVT. Expected: the session opens normally: tiles, table and traces appear.
2. Expected: no marker lines, no red banner, no popup, no console traceback in the launching shell, and no failure chip: a missing event table is not a read failure.
3. Leave it selected for 30 s. Expected: still no error; the view state chip is "archived" (or "live" for a live copy).
4. Switch to S-ARCH. Expected: its markers appear again (the "no events" state did not stick to the next session).

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-25 Chart view-state label at the top-right [BOTH] - P0
1. Expected on S-SIM: the label "live" at the top-right of the plot, in the live colour.
2. Expected: "not advancing" on S-STALE; "archived drive" on S-ARCH; "held" while held; "read failed" during a read failure (LD-07).
3. Expected: the label text and colour match the chip in the breadcrumb at every moment (they never disagree, e.g. the chart saying "live" while the chip says "held").
4. With a hover readout open the label stays visible and readable at the right edge.
5. With no fresh numeric samples at all (a session with only carried values), expected: the chart text "No fresh numeric samples yet." in the plot and never a flat trace that looks like data.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-26 Session with no chartable channels - P0
Setup: S-TEXT.
1. Select S-TEXT. Expected: the tiles appear for every channel, the "0 graphed" chip shows, and the table shows the empty-state row.
2. Expected: the chart area shows the centered text "No chartable channels in this session." (not a blank black area and not a traceback).
3. Expected: Hold (if live), Fields..., the filter and the Window combo still work; no red banner.
4. Drag a tile onto the table. Expected: refused (LD-20 step 5).
5. Open Fields.... Expected: each non-chartable channel is listed with the suffix "(<kind> channel - tile only)" or "(no numeric data yet - tile only)".
6. Right-click the chart area. Expected: no crash (no menu, or the Add field menu only).
7. Switch from S-TEXT to S-ARCH. Expected: the chart and table are fully functional again.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7B.7 Shortcuts

### LD-27 F5 rescans only while Live Data is showing - P0
1. On Live Data add a new fixture file to the sessions folder and press F5. Expected: the picker updates (same as Rescan); the selected session and the Hold state are unchanged.
2. Switch to the Dashboard with a log loaded (replay possible) and press F5. Expected: the Dashboard does what it did before (starts the replay), and Live Data does NOT rescan (add another fixture file first: it does not appear until you return to Live Data).
3. Switch to Diagnostics -> Module Map and press F5 (with the GT absent, a scan failure banner is fine). Expected: only the Diagnostics scan runs; Live Data does nothing.
4. Switch to Tuning and Active Tests and press F5. Expected: no Live Data effect.
5. Return to Live Data. Expected: F5 works again immediately.
6. Hover Rescan. Expected: tooltip ends with "(F5)".

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-28 Ctrl+F and Esc are scoped to Live Data - P0
1. On Live Data click on a tile or the chart and press Ctrl+F. Expected: focus jumps to the tile filter with its text selected.
2. Type `eng`, press Esc. Expected: the filter clears (LD-18). Press Esc again with the filter empty. Expected: nothing happens (no session switch, no dialog, no state change).
3. Click a tile (focus outside the filter) and press Esc. Expected: no effect on Hold, the selection or the layout.
4. Switch to the Dashboard and press Ctrl+F. Expected: nothing (no focus jump, the Live Data filter does not receive focus, the tab does not switch).
5. Switch to Active Tests and press Ctrl+F. Expected: nothing.
6. Switch to Tuning and press Ctrl+F. Expected: it still focuses the Tuning table search (X-06). On Diagnostics -> Codes & Readiness it focuses the code search (G-30), on Module Map nothing.
7. Return to Live Data and press Ctrl+F. Expected: the filter is focused again.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-29 Live Data keys do not leak into the Dashboard or other tabs [BOTH] - P0
1. Start a Dashboard replay or live connection (so R, M and Space have effects: Record, Mark, pause).
2. Switch to Live Data. Click a tile and the table rows, then press R, M, Space, P and the arrow keys. Expected: no recording starts, no marker is dropped, no replay pauses on the Dashboard (extends X-05); the table and the tile area ignore the keys harmlessly.
3. Type `rm ` into the filter. Expected: the letters go into the field only (no Record/Mark/pause); Space inserts a space.
4. Press F5, Ctrl+F and Esc on Live Data, then switch to the Dashboard. Expected: the Dashboard state is unchanged (no replay started or stopped by the Live Data F5, the status line and Record button as before).
5. Hold on Live Data, switch to the Dashboard. Expected: the Dashboard is unaffected, and when you return to Live Data the page is still held (or released if the drive ended).

Pass: [ ]  Fail: [ ]  Notes: ____

### 7B.8 Layout, persistence and cross-cutting

### LD-30 Page minimum width 348 px [1366] - P1
1. Launch at 1366x768 on S-ARCH with at least 20 tiles and the chart visible. Select a session with a very long label and put long text in the filter.
2. Drag the window narrower until it stops. Expected: the Live Data page minimum width is 348 px (measure on a screenshot); the window minimum width is under 1000 px (X-01).
3. At the narrowest width expected: no horizontal scrollbar on the page; the breadcrumb chips and note clip or elide; the toolbar overflows into ">>" with Rescan, Fields..., Window, Hold and the filter still reachable; the chart keeps its own minimum width and nothing paints garbage.
4. Widen to 1920x1080. Expected: tiles reflow into more columns, the chart fills the width; nothing is stretched into an unusable layout.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-31 1366x768 fit with 20+ tiles [1366] - P0
Setup: S-ARCH or S-SIM with at least 20 channels (so at least 20 tiles, ideally 24+).
1. Launch at 1366x768, maximise, open Live Data. Expected: the whole window including the status bar is on screen; no part is below the taskbar.
2. Expected: the tile area (scrolls vertically if needed), the graphed-channels table and the chart are all visible at once; the chart is not collapsed to a sliver (its minimum height is kept) and the table shows its rows or its own scroll bar.
3. Drag the splitter between the tiles and the chart both ways. Expected: neither pane can be dragged to zero (children cannot be collapsed); the tile area scrolls and the chart keeps its trace area.
4. Collapse groups (LD-17). Expected: more room goes to the chart.
5. Scroll the tile area to the bottom. Expected: the last tile is fully visible; no tile is cut off or overlapped.
6. Expected: no horizontal scrollbar on the tile area at the default width.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-32 Persistence and the keys written - P0
Setup: clean `livedata/` keys.
1. Drag the splitter to a clearly different position, collapse one group, choose a live Window of "5 min", and move one field to a new lane (so the layout is custom).
2. Quit (File -> Quit) and relaunch; open Live Data. Expected: the splitter, the collapsed group, the Window span (live sessions) and the lane layout all reopen as set. The filter text and Hold state are NOT restored (the page opens unfiltered and not held).
3. Expected in the QSettings file: `livedata/split`, `livedata/span`, `livedata/groups_collapsed`, `livedata/layout`; the page wrote no other `livedata/` keys that you cannot account for.
4. Delete the `livedata/` keys and relaunch. Expected: defaults without an error (default splitter sizes, Window "1 min" for live, all groups open).
5. Corrupt `livedata/split` (replace with garbage) and relaunch. Expected: no crash; the default split is used.
6. Select a different session. Expected: group collapse, the splitter and the layout stay (they are global, not per session).

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-33 Dark palette consistent with Tuning and Diagnostics [BOTH] - P0
1. Place Live Data next to Tuning (Editor) and Diagnostics in turn. Expected: the same breadcrumb height, the same 36 px toolbar, the same chip look, the same greys; no white or light-grey backgrounds, no default-blue selection colour, no light scroll bars (extends X-02).
2. Check the tile cards in every state (LD-15), the group headers, the filter field, the Window combo and its popup, the picker popup, the danger banner, the empty-state text, the table (swatches, header, chips), the chart canvas, the readout box, the axis and the right-click menus. Expected: all dark; text contrast at least 4.5:1, including the muted min/max footer and the faint state caption.
3. Expected: red, amber and green carry meaning only as in LD-15; the "held" chip is amber like the other warnings.
4. Open the Fields... menu. Expected: dark-themed, readable checkmarks.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-34 Switching sessions rebinds cleanly, including rapid switching - P1
1. With S-SIM, S-ARCH, S-STALE and S-NOEVT in the picker, switch through all of them one after another. Expected each time: the breadcrumb, chips, note, tiles, table rows, chart traces, markers and axis all belong to the newly selected session (no leftovers of the previous one: no stale tile, no old marker, no old banner).
2. Switch quickly (mouse wheel or arrow keys on the closed picker) 20 times. Expected: no crash, no freeze, no growing memory use or open file handles you can see in Task Manager, and the final state matches the last selection.
3. Re-select the session that is already selected. Expected: nothing is rebuilt (values keep updating, min/max keep).
4. Delete the selected session's scratch copy (S-STALE) if the OS allows it, then press F5. Expected: the picker drops it; the page falls back to the next session or the empty state without a crash. If the OS refuses the delete because the file is open, record that as an observation.

Pass: [ ]  Fail: [ ]  Notes: ____

### LD-35 Live drive end to end - P1 **[GT]**
Setup: truck on the road or a stationary run, truck-mcp logging a real drive (`session_drive_log`, source live) on the same machine, ignition ON.
1. Open Live Data and select the new session. Expected: green "live" chip, no provenance chip (real drive), tiles move with the engine, the note shows the real VIN.
2. Drive for a few minutes with a WOT pull. Expected: the footers grow, the table Min/Max/Avg change, a "wot" marker line appears on the chart when the event is recorded, and the relative time axis scrolls.
3. Hold during a pull. Expected: the display freezes within a tick; release catches up with the whole pull (no missing samples); the OpenOBD UI never touches the serial port (the logger keeps running and there is no COM port contention).
4. Stop truck-mcp logging without ending the session (kill the logger). Expected: within the stale limit the chip changes to "not advancing" and all tiles lose the live palette.
5. End the session cleanly. Expected: "archived" chip, clock time axis, Hold disabled.
6. Unplug the GT mid-drive. Expected: the tiles go to "not advancing" (not "read failed"): the session file is still readable, so Live Data reports that the logger stopped, not a read failure.

Pass: [ ]  Fail: [ ]  Notes: ____

---

**Run log addendum (Live Data)**: add the 21 P0 cases of section 7B to the P0 pass count (after 7A the total is 63; with 7B it becomes 84) and record LD-nn ids for any defects.

## 7C. Active Tests (v0.38.0)

**Covers**: v0.38.0 Active Tests tab UX overhaul (PR #57). Sources: `CHANGELOG.md` section 0.38.0, `openobd/activetests.py` (`ActiveTestsPage`), `openobd/ctljournal.py` (`control_state()` verdicts clean / outstanding / unknown), `openobd/tmstore.py` (`control_root_candidates()`), `README.md` (Active Tests paragraph), `tests/test_activetests_ui.py`. IDs are `AT-nn`. Cases marked [BOTH] are checked at 1366x768 and 1920x1080, [1366] only at 1366x768, [GT] need a live truck-mcp control session on the truck (bench decision; AT-26 only). Every other case runs against the synthetic journal fixtures below, so the whole section can be executed on the bench with no truck and no GT.

**This page is a read-only audit.** OpenOBD holds no control lease, owns no console, writes no journal and sends no frame; arm, fire and release exist only in the truck-mcp CLI. "No control exists in OpenOBD" is a P0 assertion (AT-02), not an omission.

**Preconditions addendum (Active Tests)**
- The page reads the truck-mcp control journal `<root>\control\control-journal.jsonl` (JSON lines, `at` = epoch seconds, `event`, `module`, `cpid`, other keys shown as `k=v`). It never writes it. Prepare everything in `D:\Scratch\<yyyy-mm-dd>-activetests-fixtures\` (never in the repo, never the real truck-mcp data root, never a real journal). The fixture dir below is written `$fx`.
- How the app finds journals (`tmstore.control_root_candidates()`): it ALWAYS unions four roots and never collapses to one, even under the override: (1) `%TRUCK_MCP_DATA%`, (2) `%LOCALAPPDATA%\Programs\truck-mcp-app` (field kit), (3) the hard-coded dev checkout `D:\Projects\truck-mcp`, (4) `%USERPROFILE%\truck-mcp`. Only existing directories count. There is no data-root setting in the app for this page. So `TRUCK_MCP_DATA` alone does NOT isolate the test: the real field kit and checkout journals would be mixed into the verdict. Launch OpenOBD from a PowerShell where all of these are set (shell-local, restored on closing it):
  ```
  $fx = "D:\Scratch\<yyyy-mm-dd>-activetests-fixtures"
  $env:TRUCK_MCP_DATA = "$fx\root"                 # fixture root A (journal swapped per case)
  $env:LOCALAPPDATA   = "$fx\localappdata"          # field kit root B = $fx\localappdata\Programs\truck-mcp-app
  $env:USERPROFILE    = "$fx\home"                  # root D = $fx\home\truck-mcp (leave absent unless a case says so)
  python -m openobd                                 # from the feat-activetests-ux worktree, or the deployed exe
  ```
  Create `$fx\root`, `$fx\localappdata\Programs\truck-mcp-app` and `$fx\home` as empty directories first. The one root that cannot be redirected is `D:\Projects\truck-mcp` (hard-coded): AT-01 checks it. If it holds a `control\control-journal.jsonl`, run this section on a machine or VM where it does not (do not move, edit or lock the real file) and record the build and machine here: `________`.
- Fixture generator: save the script below as `$fx\make_fixtures.py` (OUTSIDE `library\`) and run `python -I $fx\make_fixtures.py $fx\library`. It writes one `.jsonl` per fixture with timestamps relative to NOW, so re-run it before each session (and again if a case runs past 24 h). To "load" a fixture into root A: `Copy-Item $fx\library\<name>.jsonl $fx\root\control\control-journal.jsonl -Force` (create `$fx\root\control` once). The open page picks the change up within 3 s (poll) or at once with F5; relaunch is not needed.
  ```python
  import json, sys, time, pathlib
  out = pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
  N = time.time()
  def line(l):
      if isinstance(l, bytes): return l
      return (l if isinstance(l, str) else json.dumps(l)).encode()
  def w(name, lines):
      (out / (name + ".jsonl")).write_bytes(b"".join(line(l) + b"\n" for l in lines))
  arm  = {"at": N-60, "event": "arm", "module": "BCM", "cpid": "0x10"}
  act  = {"at": N-50, "event": "activate", "module": "BCM", "cpid": "0x10", "release": "AE 10 00"}
  rel  = {"at": N-40, "event": "released", "module": "BCM", "cpid": "0x10"}
  ecm  = {"at": N-30, "event": "activate", "module": "ECM", "cpid": "0x2A", "release": "AE 2A 00 00"}
  clean = [arm, act, rel]
  w("F-CLEAN", clean)
  w("F-OUT", clean + [ecm])
  w("F-DAMAGED", clean + ["{not json"])
  w("F-NONDICT", clean + ["[1, 2, 3]", "\"just a string\"", "42"])
  w("F-NOTS", clean + [{"event": "arm", "module": "TCM", "cpid": "0x05"}])
  w("F-EMPTY", [])
  w("F-NONUTF8", [b"\xff\xfe\x80\x81 garbage"])
  w("F-HOSTILE", [{"at": N-20, "event": "arm", "module": "<b>ECM</b>", "cpid": "<i>1</i>", "note": "<a href=x>y</a>"}])
  w("F-HOSTILE-OUT", [{"at": N-20, "event": "activate", "module": "<b>ECM</b>", "cpid": "<i>1</i>", "release": "<b>AE 01</b>", "note": "<img src=x onerror=alert(1)>"}])
  H = 3600
  w("F-OLD", [{"at": N-3*86400, "event": "fire_refused:no_lease", "module": "TCM", "cpid": "0x01"},
              {"at": N-3*86400+10, "event": "arm", "module": "BCM", "cpid": "0x10"},
              {"at": N-3*86400+20, "event": "activate", "module": "BCM", "cpid": "0x10", "release": "AE 10 00"},
              {"at": N-3*86400+30, "event": "return_to_normal", "module": "BCM", "cpid": "0x10"},
              {"at": N-25*H, "event": "arm", "module": "OLD25H", "cpid": "0x20"},
              {"at": N-23*H, "event": "arm", "module": "NEW23H", "cpid": "0x21"}] + clean)
  w("F-BIG", [{"at": N-60*i, "event": "arm", "module": "BCM", "cpid": "0x%02X" % (i % 200)} for i in range(150, 0, -1)])
  w("F-XA-ACT", [{"at": N-30, "event": "activate", "module": "ECM", "cpid": "0x2A", "release": "AE 2A 00 00"}])
  w("F-XB-REL-NEWER", [{"at": N-10, "event": "released", "module": "ECM", "cpid": "0x2A"}])
  w("F-XB-REL-OLDER", [{"at": N-100, "event": "released", "module": "ECM", "cpid": "0x2A"}])
  ```
- Fixture reference: **F-CLEAN** arm / activate / released on BCM 0x10; **F-OUT** F-CLEAN plus an ECM 0x2A activate with release bytes `AE 2A 00 00` and no release; **F-DAMAGED** F-CLEAN plus one malformed line; **F-NONDICT** F-CLEAN plus three valid-JSON non-object lines; **F-NOTS** F-CLEAN plus a record with no `at`; **F-EMPTY** zero bytes; **F-NONUTF8** non-UTF-8 bytes; **F-HOSTILE** / **F-HOSTILE-OUT** HTML in module, cpid, detail and release; **F-OLD** entries 3 days, 25 h and 23 h old plus F-CLEAN (9 records); **F-BIG** 150 records; **F-XA-ACT**, **F-XB-REL-NEWER**, **F-XB-REL-OLDER** for the two-journal ordering case. Missing journal = delete `$fx\root\control\control-journal.jsonl` (and keep roots B and D without one).
- Clean state: before the first case delete the `activetests/` keys from the OpenOBD QSettings file (`activetests/show`, `activetests/sources_collapsed`) so persistence cases start from defaults. Start each case from F-CLEAN loaded in root A and no journal in roots B and D unless the case says otherwise.
- Cases that change a fixture change the scratch COPY in `$fx` only. Never edit, lock or delete anything under the real truck-mcp data root, the field kit, `D:\Projects\truck-mcp` or `%USERPROFILE%\truck-mcp` of the real profile.
- Read-failure harness (AT-11 to AT-13, AT-20 step 6): a "read failed" needs `control_state()` itself to raise, which no journal file can cause (file damage is reported as "unknown", never as a failure). Save the script below as `$fx\run_failing.py` (outside the repo) and launch it INSTEAD of `python -m openobd` (same shell, same environment variables); create / delete the flag file `$fx\FAIL` to make every read raise / succeed. Set `$env:FX = $fx` and `$env:OPENOBD_SRC = "<path of the feat-activetests-ux worktree>"` before launching:
  ```python
  import os, sys
  sys.path.insert(0, os.environ["OPENOBD_SRC"])
  from openobd import ctljournal
  import openobd.app as app
  FLAG = os.path.join(os.environ["FX"], "FAIL")
  _real = ctljournal.control_state
  def _control_state():
      if os.path.exists(FLAG):
          raise OSError("simulated read failure <b>x</b>")
      return _real()
  ctljournal.control_state = _control_state
  sys.exit(app.main(["openobd"]))
  ```
  The page looks the function up at call time, so the patch takes effect on the next poll (within 3 s) or on F5.

Open Active Tests from the main tab bar (fifth tab). It is one page: the chrome (breadcrumb "Active Tests > Control journal" with chips and the note, a 36 px toolbar), a muted explainer line, an optional banner, the journal table, and the collapsible "Journal sources" panel at the bottom.

### 7C.1 Read-only guarantee and test isolation

### AT-01 Fixture isolation: only the scratch roots are read - P1
Run this first; a Fail blocks the rest of the section. Setup: environment from the Preconditions addendum, F-CLEAN in root A.
1. Before launching, run `Test-Path D:\Projects\truck-mcp\control\control-journal.jsonl`. Expected: `False`. If `True`, stop, record the machine, and move the section to a machine where it is `False` (the real file must not be touched).
2. Launch OpenOBD, open Active Tests, expand "Journal sources". Expected: the panel lists exactly the roots that exist: a `journal      <fx>\root\control\control-journal.jsonl  (3 records)` line for root A and `no journal   <fx>\localappdata\Programs\truck-mcp-app` for the field kit; no path under the real `%LOCALAPPDATA%`, the real user profile or any other drive appears (`D:\Projects\truck-mcp` is absent because it does not exist).
3. Expected: the chips "1 journal" and "3 records" match the single fixture journal; the breadcrumb note lists the same roots after "searched".

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-02 No arm / fire / activate / release control exists anywhere [BOTH] - P0
Setup: F-OUT loaded (an outstanding control is the situation where a recovery button would be most tempting). The GT is NOT connected and no truck-mcp process is running. Compute the SHA-256 of `$fx\root\control\control-journal.jsonl` now: `________`.
1. Read the toolbar. Expected: exactly Refresh, Copy report, a separator, the "Filter journal  (Ctrl+F)" field, the "Show" label and combo, and nothing else. No button, menu, checkbox or combo whose text or tooltip contains arm, fire, activate, release, recover, clear, start, run, send or test (hover every control and read the tooltip).
2. Read the whole page, including the danger banner of an outstanding control. Expected: the banner states that OpenOBD will not send the release and that truck-mcp recovery must be run; it contains no button or link. The muted line reads "No executable vehicle controls are known to OpenOBD. This page is a read-only audit of truck-mcp's control journal."
3. Open every top-level menu of the main window and every submenu. Expected: no menu item arms, fires, activates or releases a control; any items that mention Active Tests only switch tabs.
4. Right-click on a journal row, on the empty table area, on the banner, on the breadcrumb and on the Journal sources text. Expected: either no menu or only the standard text/selection menu (Copy / Select All); no control action.
5. Press F5, Enter, Space, Delete, R, M, A, F, Ctrl+Return and Ctrl+Shift+R with the table focused and with the page focused. Expected: F5 re-reads (AT-15); the others do nothing on this page.
6. Select a row and drag it. Expected: nothing is started; no drop target accepts it.
7. Recompute the SHA-256 of the journal file. Expected: identical to the value taken in Setup, and no new file appeared under `$fx\root\control\` or any other fixture root (the page does not write a journal, a lock file or a backup).
8. Expected: nothing in this case needed the GT; the status bar shows no serial activity and no connect attempt was made by opening or using the tab.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7C.2 Verdict, chips and banners

### AT-03 Clean verdict: green chip, no banner [BOTH] - P0
Setup: F-CLEAN in root A.
1. Open Active Tests. Expected: the breadcrumb reads "Active Tests > Control journal" (the product draws a chevron; "Control journal" bold). The first chip is green "clean"; then neutral "1 journal" and "3 records"; then a muted "Polling" (AT-14).
2. Expected: NO banner anywhere above the table (the green chip carries the verdict).
3. Expected: the explainer line is visible.
4. Expected: the note reads "as of HH:MM:SS . searched <root A>, ..." with the time of the latest successful read, and the searched list contains root A.
5. Expected: the table lists 3 rows, newest first (released, activate, arm).
6. Append one fresh `arm` line (current epoch) to the scratch journal. Within 3 s expected: "4 records" and 4 rows, the verdict still "clean".

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-04 Outstanding control: danger chip and banner [BOTH] - P0
Setup: page open on F-CLEAN; then load F-OUT (the chip changes live).
1. Within 3 s of the copy. Expected: the first chip becomes red "CONTROL LEFT ACTIVE" (not "clean"); counts read "1 journal" and "4 records".
2. Expected: a red (danger) banner appears above the table. It begins "CONTROL LEFT ACTIVE. A control session ended without a recorded release; the truck may be holding an actuator:" and lists, for the one outstanding control: `module ECM  CPID 0x2A  release bytes AE 2A 00 00` and a second line `journal <fx>\root\control\control-journal.jsonl`.
3. Expected: the banner ends with the sentence "OpenOBD will not send the release; run truck-mcp recovery, which replays the exact release bytes from the journal."
4. Expected: the activate row for ECM is red in the Event column; the BCM pair (activate then released) does not appear in the banner (only the unreleased control does).
5. Append `{"at": <current epoch>, "event": "activate", "module": "TCM", "cpid": "0x05", "release": "AE 05 00"}` to the scratch journal. Expected: the banner lists BOTH controls, each with module, CPID, release bytes and journal path.
6. Load F-CLEAN. Expected: the chip returns to green "clean" and the banner disappears within 3 s without restarting the app.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-05 Two journals: verdict follows wall-clock order across roots - P1
Setup: root A (`$fx\root`) gets F-XA-ACT; root B (`$fx\localappdata\Programs\truck-mcp-app\control\control-journal.jsonl`, create the `control` folder) gets F-XB-REL-NEWER.
1. Expected: chips "2 journals" and "2 records"; the verdict is green "clean": the release in the field-kit journal (10 s before generation) is newer than the activate in root A (30 s before generation), so the control is not outstanding.
2. Expected: Journal sources lists both journals, each with "1 record".
3. Replace root B's file with F-XB-REL-OLDER (release 100 s before generation, older than the activate). Expected within 3 s: red "CONTROL LEFT ACTIVE" and the banner lists ECM 0x2A with root A's journal path. An old, already-resolved release in one file must never cancel a newer activate in another.
4. Delete root B's journal. Expected: "1 journal", verdict still outstanding. Clean up: delete root B's `control` folder and load F-CLEAN in root A.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-06 Unknown (damaged line): warning chip and banner, never clean [BOTH] - P0
Setup: F-DAMAGED in root A.
1. Expected: the first chip is amber "journal unknown" (warning), NOT green "clean"; counts "1 journal", "3 records" (the damaged line is not counted as a record).
2. Expected: a warning banner (amber, visibly different from the red danger banner of AT-04) begins "CONTROL STATE UNKNOWN. Journal damaged or partly unreadable" and names `control-journal.jsonl: 1 damaged line(s)`.
3. Expand Journal sources. Expected: the journal line reads `journal      <path>  (3 records, 1 damaged line(s))`.
4. Expected: the 3 readable rows are still listed.
5. Load F-NONUTF8. Expected: chip "journal unknown"; the banner mentions the file name with a decoding error; Journal sources shows `unreadable   <path>  (<reason>)`; "0 records"; the table shows the "No journal entries" row.
6. Load F-CLEAN. Expected: the chip returns to "clean" and the banner is gone.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-07 Unknown (missing journal): not the same as nothing commanded [BOTH] - P0
Setup: delete the journal in root A; roots B and D have none.
1. Expected: the first chip is amber "journal unknown" (not "clean"); chips "0 journals" and "0 records".
2. Expected: a warning banner "CONTROL STATE UNKNOWN. No control journal located in any truck-mcp data root. Control state cannot be confirmed" followed by the sentence that this is not the same as "nothing is commanded".
3. Expected: the table shows the single muted row "No journal entries"; the explainer line is still shown.
4. Expand Journal sources. Expected: one `no journal   <root>` line per existing root (root A, field kit root); no root is hidden.
5. Copy F-CLEAN into root A. Expected within 3 s: chip "clean", banner gone, 3 rows.
6. Delete the whole `$fx\root` folder while the page is open. Expected: still "journal unknown" with no crash; Journal sources no longer lists the removed root (only existing directories are searched). Recreate `$fx\root\control` afterwards.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-08 Non-dict journal line is damage, not data - P0
Setup: F-NONDICT in root A (lines `[1, 2, 3]`, `"just a string"`, `42` are valid JSON but not records).
1. Expected: chip amber "journal unknown", NOT "clean"; "3 records" (only the 3 real records); the banner reports `3 damaged line(s)`.
2. Expected: none of the three non-dict lines appears as a table row, and the table does not crash or show an empty/garbled row.
3. Expand Journal sources. Expected: `(3 records, 3 damaged line(s))`.
4. Load F-CLEAN. Expected: back to green "clean".

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-09 Record with no usable timestamp downgrades to unknown - P1
Setup: F-NOTS in root A.
1. Expected: chip amber "journal unknown"; banner includes "1 record(s) with no usable timestamp"; "4 records".
2. Expected: the table lists the record without a timestamp LAST (below the oldest dated row), with an empty When cell; no crash.
3. Load F-CLEAN. Expected: green "clean".

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-10 Empty journal file - P1
Setup: F-EMPTY (zero bytes) in root A.
1. Expected: the journal exists and is readable: green "clean", "1 journal", "0 records", no banner. (An empty journal is not damage; this is different from a missing journal, AT-07.)
2. Expected: the table shows the muted row "No journal entries"; the row cannot be selected.
3. Type `x` in the filter. Expected: the "No journal entries" row stays visible (the filter never hides it); clear with Esc.
4. Load F-CLEAN. Expected: the three rows replace the empty-state row.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7C.3 Read failure honesty

### AT-11 Failed read keeps the table but never presents it as current [BOTH] - P0
Setup: launch via the read-failure harness (`run_failing.py`), no `FAIL` flag, F-CLEAN loaded. Note the time in the note: `________`.
1. Create the flag file `$fx\FAIL` and press F5. Expected: the first chip is red "read failed" (danger); the green "clean" chip is gone (a stale all-clear is never shown).
2. Expected: the note reads `Read failed: simulated read failure <b>x</b> . last good read HH:MM:SS` where HH:MM:SS is the time noted before the failure, not the current time, and the `<b>x</b>` is shown literally.
3. Expected: a red banner reads "Could not read the control journal: simulated read failure <b>x</b>. The table below is the last good read (HH:MM:SS), not current."
4. Expected: the 3 rows from the last good read are still in the table (the table is kept, the banner and chip label it).
5. Leave the flag for 20 s. Expected: the last good time does NOT advance (a failed poll is never "as of now"); the chip, banner and rows remain.
6. Delete the flag, wait up to 3 s (or F5). Expected: the "read failed" chip, the banner and the "last good read" note are gone; chip "clean"; the note reads "as of <new time>".

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-12 Failed read keeps an unsafe last-good verdict visible; recovery - P0
Setup: harness, F-OUT loaded, page showing the red "CONTROL LEFT ACTIVE" banner.
1. Create `$fx\FAIL`; press F5. Expected: chips "read failed" (red) AND "CONTROL LEFT ACTIVE" (red) both present; the page does not fall back to a green or empty state.
2. Expected: the banner contains BOTH the read-failure paragraph and the CONTROL LEFT ACTIVE paragraph with the CPID `0x2A`, release bytes `AE 2A 00 00`, the journal path and the sentence "OpenOBD will not send the release".
3. While failing, load F-CLEAN into root A, then delete the flag. Expected: after the next successful read the unsafe verdict clears: chip "clean", no banner, new rows. (The verdict is never changed by a failed read, only by a successful one.)
4. Repeat steps 1 and 2 starting from F-DAMAGED (unknown). Expected: "read failed" plus the amber "journal unknown" chip; the banner shows the read-failure paragraph only (the unknown warning is not repeated while a failure is shown).
5. Repeat from F-CLEAN. Expected: "read failed" only, with no "clean" chip (as AT-11 step 1).

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-13 First read fails: nothing to show, nothing implied - P1
Setup: harness, create `$fx\FAIL` BEFORE launching, F-CLEAN loaded.
1. Launch and open Active Tests. Expected: chip "read failed" (red); NO "clean", journal-count or record-count chip.
2. Expected: the note reads "Read failed: simulated read failure <b>x</b> . no successful read yet"; the banner reads "Could not read the control journal: ... No journal has been read yet."
3. Expected: the table is empty (not a "No journal entries" row implying an empty journal) and Journal sources, when expanded, has no lines (no root is shown as read).
4. Delete the flag and press F5. Expected: normal green "clean" display, the failure text gone.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7C.4 Chrome, polling and shortcuts

### AT-14 Breadcrumb, chips, note and "Polling" only while shown [BOTH] - P1
1. With Active Tests showing, read the chips. Expected: verdict chip, "n journal(s)" and "n record(s)" (singular "1 journal", "1 record"; plural otherwise), then a muted "Polling".
2. Note the "as of" time. Without touching anything wait 10 s. Expected: the time advances about every 3 s (poll); the note does not widen the page.
3. Change the fixture (F-CLEAN to F-OUT). Expected: within 3 s the chips, banner and rows update with no click.
4. Switch to the Dashboard (or Live Data), change the fixture back, wait 10 s, switch back. Expected: on return the page refreshes at once (the "as of" time is fresh) and the "Polling" chip is present again.
5. Expected: polling runs only while the tab is showing (see AT-15 step 4); the "Polling" chip is present only while the page is shown.
6. Narrow the window. Expected: chips and note elide; they do not widen the page. Hover the elided note. Expected: a tooltip shows the full text including every searched root.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-15 F5 refreshes only while Active Tests shows - P0
Setup: F-CLEAN; Process Monitor (Sysinternals) optional for step 4.
1. Hover Refresh. Expected: tooltip "Re-read every truck-mcp control journal now  (F5)"; Refresh is the highlighted (primary) button.
2. With Active Tests showing, load F-OUT and press F5 within a second of the last poll. Expected: the chip, banner and rows change immediately (not after the 3 s poll).
3. Click Refresh with F-CLEAN loaded. Expected: the same re-read; the "as of" time updates.
4. Switch to the Dashboard and press F5. Expected: it does what it did before (starts a replay when a log is loaded; nothing otherwise). On Diagnostics press F5: Expected: it does what G-29 says (scan / read), and nothing on Active Tests. On Live Data F5 is its Rescan. With Process Monitor filtered on `control-journal.jsonl`, stay on the Dashboard for 10 s and press F5 several times. Expected: no read of the journal at all while the tab is hidden (no F5 re-read, no poll). If Process Monitor is unavailable, record N/A for the Process Monitor part (covered by `test_f5_and_polling_only_while_showing`).
5. Return to Active Tests and press F5. Expected: refresh works again.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-16 Ctrl+F focuses the journal filter only here; Esc clears - P0
1. On Active Tests click the table and press Ctrl+F. Expected: the filter field takes focus with its text selected.
2. Type `ecm`, press Esc. Expected: the field clears and every row returns. Press Esc again with the field empty. Expected: nothing else happens (no tab switch, no dialog).
3. With text in the field click the table and press Esc. Expected: the text stays (Esc clears only from inside the field).
4. Click the field's clear button. Expected: same as Esc in step 2.
5. Switch to the Dashboard and press Ctrl+F. Expected: nothing. On Tuning (Editor) Expected: it still focuses the table search (X-06). On Diagnostics Codes & Readiness it focuses the code search (G-30); on Module Map nothing. On Live Data it focuses the tile filter. None of these touches the journal filter.
6. Switch back to Active Tests. Expected: the filter keeps whatever text it had; Ctrl+F works again.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7C.5 Filter and Show (display only)

### AT-17 Journal filter is display-only - P0
Setup: F-OUT loaded (red chip, 4 rows).
1. Type `ecm` (lower case). Expected: only the ECM activate row remains; all 4 records still counted in the chips ("4 records"); the verdict chip stays red "CONTROL LEFT ACTIVE" and the banner still lists the ECM control.
2. Type `ECM` / `Ecm`. Expected: case-insensitive, same result.
3. Type `release=` and then `AE 10`. Expected: the filter matches the Detail text (`release=AE 10 00`).
4. Type `released` and then `activate`. Expected: matches the Event column.
5. Type `0x2A` (the CPID). Expected: the ECM row remains (CPID text also matches); the verdict is unchanged. Record this if the CPID is NOT matched.
6. Type nonsense. Expected: no rows, no crash, no "No journal entries" row, chips and banner unchanged.
7. With `ecm` still typed load F-CLEAN, wait 3 s. Expected: the filter is re-applied to the new data (nothing matches `ecm`; the filtered table is empty) while the verdict is "clean" and "3 records". Clear with Esc: 3 rows appear.
8. Filter to one row, click Copy report. Expected: the report's "Entries shown" count is the visible count (see AT-20) while its "Verdict" and "records" lines still describe the whole journal.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-18 Show combo: modes, persistence, never changes the verdict - P0
Setup: F-OLD loaded (3-day-old fire_refused:no_lease on TCM, BCM pair, `OLD25H` arm 25 h ago, `NEW23H` arm 23 h ago, plus F-CLEAN). `activetests/show` deleted.
1. Open the combo. Expected: three entries "All entries", "Activate + refused only", "Last 24 h"; the default is "All entries" and lists every record (newest first; "9 records" chip).
2. Choose "Activate + refused only". Expected: only `activate` rows and `fire_refused...` rows remain (BCM activate x2, TCM `fire_refused:no_lease`); `arm`, `released` and `return_to_normal` rows are hidden.
3. Choose "Last 24 h". Expected: `OLD25H` and everything older than 24 h are gone; `NEW23H` and the three F-CLEAN rows remain.
4. For every mode expected: the verdict chip stays "clean", the record chip stays "9 records" and no banner appears or disappears (Show is display only).
5. Quit (File -> Quit) with "Last 24 h" chosen and relaunch. Expected: the combo reopens on "Last 24 h"; the QSettings file holds `activetests/show` = `24h`.
6. Delete the `activetests/show` key and relaunch. Expected: "All entries" without error. Set the key to garbage (`activetests/show` = `zzz`) and relaunch. Expected: "All entries", no crash.
7. In "Last 24 h", load F-OUT (an outstanding control from 30 s ago). Expected: the ECM row is visible and the red banner shows. Now load a copy of F-OLD edited so its 3-day-old `activate` line has module `TCM` and its `return_to_normal` line is deleted (no later release matches it). Expected: the chip is red "CONTROL LEFT ACTIVE" and the banner lists the TCM control even though the "Last 24 h" table hides that old row (an old unreleased control must not be hidden by the display mode).

Pass: [ ]  Fail: [ ]  Notes: ____

### 7C.6 Journal table, report and sources

### AT-19 Journal table anatomy: order, date, colours, detail, cap [BOTH] - P1
Setup: F-OLD loaded, "All entries".
1. Expected columns left to right: When, Event, Module, CPID, Detail. Rows are newest first.
2. Expected: the first row (and the first row after the calendar day changes) shows the full "YYYY-MM-DD HH:MM:SS"; consecutive rows on the same calendar day show only "HH:MM:SS". Check across the 3-day-old rows and the 25 h / 23 h rows.
3. Expected Event colours: `activate` and `fire_refused:no_lease` red; `released` and `return_to_normal` green; `arm` neutral (default text colour). The event word is also written out, so colour is never the only cue.
4. Expected: Detail shows the remaining keys as `k=v` joined with ", " (e.g. `release=AE 10 00`); the `at`, `event`, `module` and `cpid` keys are not repeated in Detail.
5. Hover the When cell: tooltip shows the full timestamp. Hover the Detail cell: tooltip shows the detail and "journal: <path>".
6. Load F-BIG (150 records). Expected: the chip reads "150 records" but the table lists only the newest 100 rows. Record that limit as observed behaviour (the cap is by design); scroll to the end: the oldest listed row is the 100th newest.
7. Select several rows with Shift+click and Ctrl+click. Expected: extended selection works; selecting changes nothing else.
8. Resize the columns narrower. Expected: When, Event, Module and CPID size to contents; Detail takes the rest and elides; no horizontal growth of the page.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-20 Copy report: plain-text summary - P1
Setup: F-OUT loaded, filter `ecm` typed.
1. Hover Copy report. Expected: tooltip names the verdict, any outstanding control and listed entries. Click it, paste into Notepad.
2. Expected first line "OpenOBD Active Tests - control journal report (read-only)", then "Last successful read: YYYY-MM-DD HH:MM:SS", then "Verdict: CONTROL LEFT ACTIVE", the detail line, "Journals: 1, records: 4".
3. Expected: an "Outstanding controls:" block with `module ECM  CPID 0x2A  release bytes AE 2A 00 00  journal <path>` followed by the sentence "OpenOBD will not send the release; run truck-mcp recovery...".
4. Expected: a "Journal sources:" block with every searched root and its record count / damage (including the roots without a journal).
5. Expected: "Entries shown (1, newest first):" followed by exactly the visible (filtered) rows, as plain columns separated by two spaces. Clear the filter, copy again: "Entries shown (4, newest first)".
6. Load F-CLEAN: the report has no "Outstanding controls" block. Run it from the failure harness with the flag set (AT-11): the report starts with "Read failed: ..." and shows the last-good time.
7. Paste the F-HOSTILE report. Expected: the hostile strings appear verbatim in the text.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-21 Journal sources panel and persisted settings - P1
1. Fresh settings: open Active Tests. Expected: "Journal sources" is COLLAPSED (disclosure button only).
2. Expand it. Expected: plain-text lines, one per searched root: `journal      <path>  (n records)` (plus `, n damaged line(s)` when relevant), `no journal   <root>`, or `unreadable   <path>  (<error>)`. The text can be selected with the mouse and copied.
3. Put F-CLEAN in root A and F-DAMAGED in root B (field kit, `control\control-journal.jsonl`). Expected: one `journal` line with "3 records" and one with "3 records, 1 damaged line(s)"; chips "2 journals" and "6 records"; the verdict "journal unknown". Remove root B's journal afterwards.
4. Quit and relaunch. Expected: the panel reopens expanded (the choice is persisted); collapse it, relaunch, expect collapsed.
5. Check the QSettings file. Expected: the only keys written by this page are `activetests/show` and `activetests/sources_collapsed` (`false` when expanded, `true` when collapsed).
6. Delete both keys and relaunch. Expected: defaults (All entries, sources collapsed), no error.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7C.7 Hostile text, layout and theme

### AT-22 Hostile strings render literally, everywhere - P0
Setup: F-HOSTILE-OUT in root A (outstanding control with `<b>ECM</b>` module, `<i>1</i>` CPID, `<b>AE 01</b>` release bytes, `<img ... onerror=alert(1)>` detail). Also rename the fixture root folder to `a&b%41` (update `TRUCK_MCP_DATA` to `$fx\a&b%41` and relaunch) so a root path contains `&` and `%41`.
1. Expected: no bold or italic text and no image anywhere; no dialog; no network request. The table cells show the raw text `<b>ECM</b>`, `<i>1</i>` and the detail `note=<img src=x onerror=alert(1)>` as typed.
2. Expected: the danger banner shows `module <b>ECM</b>  CPID <i>1</i>  release bytes <b>AE 01</b>` and the journal path with `&` and `%41` literally (no entity decoding, no formatting).
3. Hover the Module and Detail cells. Expected: the tooltip also shows the literal text (no rendered markup, no link).
4. Expand Journal sources and read the note. Expected: raw text, the `&` shown as `&`, not as an entity.
5. Type `<b>` in the filter. Expected: the hostile row matches and stays; nothing renders.
6. Click Copy report and paste into Notepad. Expected: the hostile strings appear verbatim.
7. Load F-HOSTILE (clean fixture). Expected: chip "clean"; the `<b>ECM</b>` row is listed literally; no banner.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-23 Minimum width at 1366x768; long paths elide [1366] - P0
Setup: copy F-OUT into a deep fixture root so the journal path is very long (create e.g. `$fx\root-with-a-very-long-folder-name-for-width-testing\second-very-long-nested-folder-name\control\control-journal.jsonl`, keep the path under 260 characters, point `TRUCK_MCP_DATA` at the parent of `control` and relaunch), expand Journal sources.
1. Launch at 1366x768, maximise. Expected: no horizontal scrollbar; the whole window including the status bar fits the screen.
2. With the outstanding banner (long journal path), the expanded sources panel and all chips showing, drag the window narrower until it stops. Expected: the Active Tests page minimum width is under 700 px and the window minimum width is under 1000 px (measure); the banner and sources text clip at the right edge instead of widening the page (the full path is in Copy report); chips and note elide.
3. At the narrowest width expected: the toolbar stays usable (">>" overflow if needed) and the filter field and Show combo are reachable.
4. Check 1920x1080. Expected: the table fills the area; nothing is stretched into an unusable layout.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-24 Dark palette consistent with the other tabs; state is not colour-only [BOTH] - P1
1. Place Active Tests next to Tuning, Diagnostics and Live Data in turn. Expected: the same breadcrumb height, the same 36 px toolbar, the same chip look, the same greys; no white or light-grey backgrounds, no default-blue selection, no light scroll bars (extends X-02).
2. Load, in turn, F-CLEAN, F-OUT, F-DAMAGED and the failure harness state. Expected: green chip (clean), red banner (outstanding / read failed), amber banner (unknown) are three distinguishable looks, each with text contrast of at least 4.5:1; each state is also stated in words in the chip.
3. Check the table in all four states: red / green event text readable on the dark row background, selected row readable.
4. Check the muted explainer, the "No journal entries" row and the disclosure button. Expected: all in the dark palette.

Pass: [ ]  Fail: [ ]  Notes: ____

### AT-25 Keyboard-only pass - P2
1. Without the mouse: Tab from the tab bar to Refresh (Enter), Copy report (Space), the filter, the Show combo (arrow keys), the table (arrow keys, Ctrl+A), and the Journal sources disclosure button (Space).
2. Expected: every control works and shows a visible focus indicator; Ctrl+F then typing then Esc works; the table rows are selectable by keyboard.
3. Expected: no keyboard path arms, fires, activates or releases anything (AT-02).

Pass: [ ]  Fail: [ ]

### 7C.8 Live truck-mcp session

### AT-26 Live control session seen from OpenOBD (read-only) [GT] - P2 (bench truck only, Ron's decision)
Setup: ONLY if Ron chooses to run a real control session with the truck-mcp CLI on the bench truck, following truck-mcp's own control runbook (arm behind its console nonce, one supported activation, release). OpenOBD is open on Active Tests with the REAL data root (launch without the fixture environment variables for this case only) and is NOT used to send anything. Record N/A if not run.
1. Before the session. Expected: green "clean" (or amber "journal unknown" if no journal yet); no banner.
2. In the truck-mcp CLI arm and activate the control. Expected: within 3 s a new red `activate` row, chip "CONTROL LEFT ACTIVE" and the danger banner with the real module, CPID, release bytes and journal path; OpenOBD shows no button to release.
3. Release the control from the truck-mcp CLI (never from OpenOBD). Expected: within 3 s a green `released` row, the banner disappears and the chip returns to "clean"; "n records" increased.
4. Expected: OpenOBD opened no serial port and sent no frame during the session; the journal file was only appended to by truck-mcp.
5. If the session is cut short or the CLI is closed with a control still active, do NOT use OpenOBD to recover: run truck-mcp recovery and confirm the page returns to "clean" afterwards.

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

---

**Run log addendum (Active Tests)**: add the 14 P0 cases of section 7C to the P0 pass count (after 7A and 7B the total is 84; with 7C it becomes 98) and record AT-nn ids for any defects.

## 7D. Log folder and Open session (v0.39.0)

**Covers**: v0.39.0 configurable log folder plus Live Data "Open session..." (PR #60). Sources: `CHANGELOG.md` section 0.39.0, `openobd/recorder.py` (`log_dir`, `probe_log_dir`, `log_dir_unavailable`, `LOG_DIR_KEY`), `openobd/app.py` (`configured_log_dir`, Settings -> "Log folder..." and "Reset log folder to default", Preferences "Log folder:" row, recording start / finalise status and Record tooltip, `_log_browse_dir`), `openobd/livedata.py` (Open session... toolbar button with the recent-files arrow menu, "file . <name>" picker entries, "opened file" chip, path in the note, Hold, danger banner), `tests/test_recorder_dir.py`, and the v0.39.0 tests at the end of `tests/test_livedata_ui.py`, as amended by the fix round on this branch. IDs are `LF-nn`. Cases marked [BOTH] are checked at 1366x768 and 1920x1080, [1366] only at 1366x768, [GT] need the OBDX Pro GT on the truck (recording only starts from a live GT). Every other case runs on the bench with no truck and no GT: the log-folder setting, picker refusal, Load Log and the whole of Open session are fully testable without the GT. The fallback behaviour at recording start (LF-11, LF-24) and the GTPro recording (LF-09) are the cases that need it.

**What this section protects.** Two promises. (1) A recording is never silently saved somewhere other than where the user was told: when the chosen folder cannot be used (missing, unwritable, or not an absolute path) the file goes to the default folder and BOTH the status line and the Record tooltip say so, naming the folder that was skipped. (2) A session file opened from disk is treated exactly like a discovered session: its chip is derived from the data, never from how it was opened. It reads "live" only while its newest sample is advancing and the drive has not ended, "not advancing" when the writer has stopped without ending it, and "archived" once the drive has ended. An "opened file" chip and the file's full path sit on top of the normal chips. A file opened mid-drive therefore never reads "archived" while it advances, and a stopped or copied file never reads "live" indefinitely. The "as of" time in the note, for EVERY session (discovered or opened), is the timestamp of the newest sample, never the wall-clock time of the last read; a session with no samples reads "no samples yet". A file that is not a readable session never replaces the session that was bound. The P0 cases are those honesty, persistence and refusal assertions.

**Connection semantics (as decided in the fix round).** An opened file is opened without locking; OpenOBD never writes rows, but SQLite may create WAL sidecar files (-wal/-shm) next to the file while it is open. So the integrity check for "read-only" is the SHA-256 of the `.tmsession.db` itself only; a `-wal` / `-shm` appearing beside it is allowed and is not a failure.

**Preconditions addendum (Log folder and Open session)**
- Scratch area: `D:\Scratch\<yyyy-mm-dd>-logfolder-fixtures\` (never in the repo, never the real truck-mcp data root). Below it is written `$fx`. Create these empty folders first: `$fx\truck-mcp\sessions` (the data root OpenOBD scans), `$fx\picked` (where "opened" session files live: deliberately NOT under any scanned sessions folder), `$fx\lf` (log-folder fixtures). Launch OpenOBD from a PowerShell where `TRUCK_MCP_DATA` is shell-local, so Live Data scans only the scratch root:
  ```
  $fx = "D:\Scratch\<yyyy-mm-dd>-logfolder-fixtures"
  $env:TRUCK_MCP_DATA = "$fx\truck-mcp"
  python -m openobd            # from the feat-log-folder-open-session worktree, or the deployed exe
  ```
  The app has no setting for the scanned root; `TRUCK_MCP_DATA` is authoritative and alone for Live Data (the exception is LF-20 step 8, which points it at the logger's root on purpose).
- Ron's real target folder for the GT recordings is `C:\Users\ohio_\OneDrive\Personal\Vehicle\GTPro` (exists, empty, OneDrive-synced). It is used only by LF-02, LF-06, LF-09 and LF-12. Before LF-02 confirm it is empty and note it: `________`. The test recordings made there are test data: after LF-12, delete them (Recycle Bin) unless Ron wants them, so the folder is left as found, and finish the section by setting the log folder the way Ron wants it left: `________`.
- Where truck-mcp session files live: `<TRUCK_MCP_DATA or %LOCALAPPDATA%\Programs\truck-mcp-app>\sessions\*.tmsession.db` (the field kit; a dev checkout keeps them under `D:\Projects\truck-mcp\sessions\`). Never open, edit, lock or delete a file under the real data root for these cases: COPY it to `$fx\picked` first (Copy-Item). If a file has `-wal` / `-shm` companions beside it (a drive still being written), copy those too or copy only after the drive ended; a bare copy of a mid-write database can lack the newest samples.
- Open-session fixtures (record the file names: `________`):
  - **OS-ARCH**: a copy of an ended drive (`session.ended_utc` set) in `$fx\picked`, renamed `os-arch.tmsession.db`. Reuse the 7B S-ARCH copy. Note its newest sample timestamp (the last point of any channel in truck-mcp's session tools, or the last chart point after opening): `________`.
  - **OS-B** ... **OS-J**: nine further copies of OS-ARCH in `$fx\picked`, each renamed with a distinct name (`os-b.tmsession.db` to `os-j.tmsession.db`). The copies need a distinct `session.uid` only if the picker complains; they are never opened at the same time as their source.
  - **OS-EMPTY**: `$fx\picked\os-empty.tmsession.db`, a session with metadata and channels but no samples yet: the first second of a fresh truck-mcp drive log, or a scratch COPY of OS-ARCH whose sample rows were removed with `python -I` and `sqlite3` on the COPY only.
  - **OS-BAD**: `$fx\picked\not-a-session.tmsession.db`, a plain CSV (any OpenOBD recording, or three lines of text) renamed to that name.
  - **OS-LIVE**: the live drive of LF-15 and LF-20 (truck-mcp `session_drive_log`, simulated source is fine; no GT is needed). Its logger writes to its OWN data root; only LF-15, LF-20 and LF-23 use it.
  - **OS-HOSTILE**: a copy of OS-ARCH at `$fx\picked\a&lt;b &amp; 100%41\x.tmsession.db` (folder name contains `&`, `;`, `%41` and the text `&lt;`; Windows forbids `<` and `>` in names).
- Folder fixtures (LF-05, LF-08, LF-11), made inside `$fx\lf`: `readonly` (write denied by ACL, below), `vol` (a plain folder), and `a&amp;b %41 &lt;i&gt;x&lt;/i&gt;` (markup-looking text in the name; a folder named with a literal `&`, `;`, `%41` and the characters `lt;`, valid on Windows). Deny writes with:
  ```
  New-Item -ItemType Directory "$fx\lf\readonly"
  icacls "$fx\lf\readonly" /deny "${env:USERNAME}:(OI)(CI)(WD,AD)"
  # cleanup after LF-05, before deleting the fixture area:
  icacls "$fx\lf\readonly" /remove:d $env:USERNAME
  ```
  Read-only FILE attributes do not stop file creation inside a Windows folder, so use the ACL deny, a write-protected removable drive, or a drive letter that does not exist; the read-only attribute alone is not a valid fixture.
- Clean state: before the first case delete the `logs/dir` and `livedata/recent_files` keys from the OpenOBD settings file, so the defaults apply. Quit OpenOBD (File -> Quit) before editing the file and relaunch afterwards. The only keys this section's features write are `logs/dir` (an absolute path string; absent after Reset) and `livedata/recent_files` (a list of at most 8 paths).
- Wording the product draws that this document writes in ASCII: the picker entry is `file`, a middle dot, then the file name (written `file . <name>`); status text uses an arrow and an em dash (written `->` and `-`); the Settings items end in an ellipsis (written `...`).
- Default log folder: `Documents\OpenOBD\Logs`, resolved by Windows known-folder lookup, so on a OneDrive-redirected Documents it is under OneDrive. Note it here: `________`.

### 7D.1 Setting the log folder

### LF-01 Defaults and controls [BOTH] - P1
Setup: clean state (no `logs/dir` key).
1. Open the Settings menu. Expected: an item "Log folder..." and an item "Reset log folder to default" are present (next to the other Settings items, not on the File menu).
2. Settings -> Preferences... (Ctrl+,). Expected: a row labelled "Log folder:" shows `<default folder> (default)` as plain text, with the full path in its tooltip, followed by two buttons "Change..." and "Reset to default".
3. Expected: the folder named in the row is the default noted in the Preconditions (the real Documents folder, not a hard-coded `C:\Users\<name>\Documents` when Documents is redirected).
4. Close Preferences with Cancel. Expected: no `logs/dir` key was written.
5. Repeat steps 1 and 2 at 1366x768 and 1920x1080. Expected: the row fits the dialog; nothing is clipped at 1366x768.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-02 Set the folder from Settings -> Log folder... (GTPro) - P0
Setup: clean state; GTPro empty (noted in Preconditions).
1. Settings -> Log folder.... Expected: a folder chooser titled "Log folder" opens, starting in the default folder (or the Documents area), not in an unrelated place.
2. Choose `C:\Users\ohio_\OneDrive\Personal\Vehicle\GTPro` and confirm. Expected: the chooser closes with no error box; the status bar shows `Log folder: C:\Users\ohio_\OneDrive\Personal\Vehicle\GTPro` for about 6 s.
3. Open Settings -> Preferences.... Expected: the row shows exactly that path, without the "(default)" suffix.
4. Look in GTPro in Explorer. Expected: the folder is still empty: the write probe (`.openobd_write_probe_<pid>`) was created and deleted and nothing is left behind, and OneDrive shows no sync error for the folder.
5. Check the settings file. Expected: `logs/dir` = `C:\Users\ohio_\OneDrive\Personal\Vehicle\GTPro` (normalised backslashes, absolute), and no other key was changed.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-03 Set and reset from the Preferences row - P1
Setup: clean state.
1. Settings -> Preferences..., click "Change...". Expected: the same "Log folder" chooser opens.
2. Choose `$fx\lf\vol`. Expected: the row text changes at once to that path (before the dialog is closed); the status bar shows `Log folder: $fx\lf\vol`.
3. Close Preferences with Cancel (not OK). Expected: the folder STAYS set (the row applies immediately, like the Settings menu items); reopen Preferences to confirm.
4. Click "Reset to default". Expected: the row changes at once to `<default folder> (default)`; the status bar shows `Log folder: <default folder> (default)`; the `logs/dir` key is removed (not left empty).
5. Click "Change..." and choose a folder that does not yet exist by using the chooser's New Folder button (e.g. `$fx\lf\made-in-dialog`). Expected: the folder is accepted and shown.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-04 Cancelling the chooser changes nothing - P1
Setup: log folder set to `$fx\lf\vol`.
1. Settings -> Log folder..., then Cancel. Expected: no message box; status bar unchanged; `logs/dir` still `$fx\lf\vol`.
2. Settings -> Preferences... -> "Change...", then Cancel. Expected: the row still shows `$fx\lf\vol`.
3. Press Esc in the chooser instead of Cancel; close it with the window's X. Expected: the same.
4. Repeat step 1 with the folder at the default (no key). Expected: still no `logs/dir` key written (Cancel does not pin the default into the settings).

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-05 A folder that cannot be written is refused by the chooser - P0
Setup: `$fx\lf\readonly` with the ACL deny from the Preconditions. Set the log folder to `$fx\lf\vol` first.
1. Settings -> Log folder... and choose `$fx\lf\readonly`. Expected: a warning box titled "Log folder": "Recordings cannot be written to:", then the folder, then the reason (a PermissionError / access-denied text), then "The log folder is unchanged.". No status-bar `Log folder:` message.
2. Dismiss it and open Preferences. Expected: the row still shows `$fx\lf\vol`; `logs/dir` is unchanged (a refused folder is never stored, not even temporarily).
3. Repeat from Preferences -> "Change...". Expected: the same warning and the row is unchanged.
4. Repeat with the log folder at the default (no key). Expected: the warning appears and the setting stays at the default (the key is still absent).
5. Optional, only if a removable USB drive is at hand: choose a folder on it, unplug the drive, then try to choose the same folder path again by typing it in the chooser. Expected: it cannot be chosen (missing drive), or if the chooser lets it through it is refused with the same warning. Record the observed behaviour: `________`.
6. Clean up: `icacls "$fx\lf\readonly" /remove:d $env:USERNAME`.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-06 The setting survives a restart - P0
Setup: log folder set via Settings to GTPro (LF-02).
1. Quit (File -> Quit) and relaunch. Open Preferences. Expected: the row shows the GTPro path; Settings -> Log folder... starts the chooser inside GTPro (its parent area).
2. Expected: Load Log (LF-12) also still opens in GTPro (checked there).
3. Quit; in the settings file set `logs/dir` to `   ` (spaces only) and relaunch. Expected: no crash; the row shows `<default folder> (default)`; recording would use the default.
4. Quit; set `logs/dir` to a non-string such as an integer `5` (or an empty list) and relaunch. Expected: no crash; default shown.
5. Quit; delete the key and relaunch. Expected: default shown, no error. Re-set GTPro through the Settings menu afterwards if the next case needs it.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-07 Reset log folder to default (Settings menu) - P0
Setup: log folder set to GTPro.
1. Settings -> Reset log folder to default. Expected: no confirmation box; the status bar shows `Log folder: <default folder> (default)` for about 6 s, naming `Documents\OpenOBD\Logs` (the resolved path from the Preconditions).
2. Preferences. Expected: the row shows `<default folder> (default)`.
3. Check the settings file. Expected: the `logs/dir` key is gone.
4. Quit and relaunch. Expected: still the default (a reset survives a restart and does not come back).
5. Run Reset again with nothing set. Expected: no error; the same status text.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-08 Folder text is plain text and long paths elide [1366] - P1
Setup: set the log folder to `$fx\lf\a&amp;b %41 &lt;i&gt;x&lt;/i&gt;` (create it first through the chooser's New Folder button).
1. Preferences. Expected: the row shows the folder name exactly as typed, `&`, `%41` and the `&lt;i&gt;` text literal; nothing is italic, no `&amp;` becomes `&`, no mnemonic underline, and `%41` is NOT expanded to `A` or otherwise treated as a variable.
2. Hover the row. Expected: the tooltip shows the same literal path.
3. Read the status bar message from the choose action (re-choose the folder if it has gone). Expected: literal text.
4. Set the log folder to a deep path of about 200 characters (nest folders such as `$fx\lf\a-very-long-folder-name-0123456789\...`). At 1366x768 open Preferences. Expected: the row elides the path with an ellipsis, does not widen the dialog beyond the screen, and the full path is in the tooltip.
5. With the markup folder set, start a recording if a GT is available (see LF-09 for the steps). Expected: the Record button tooltip, status line and post-Stop status all show the folder text literally. Record N/A if no GT.

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### 7D.2 Recording and Load Log with the log folder

### LF-09 Record into GTPro; the CSV and .meta.json land there and are named [GT] - P0
Setup: GT connected on the truck (ignition on, Dashboard live); log folder set to GTPro (LF-02); GTPro empty; note the time: `________`.
1. Hover the Record button before starting. Expected: it is the red RECORD button, enabled.
2. Start a recording (click RECORD or press R) and let it run about 20 s. Expected: the status line reads `Recording -> C:\Users\ohio_\OneDrive\Personal\Vehicle\GTPro\<file>.csv`, and no "Log folder unavailable" text anywhere.
3. Hover the Record button while recording. Expected: the tooltip starts `Recording to C:\Users\ohio_\OneDrive\Personal\Vehicle\GTPro\<file>.csv`.
4. Look in GTPro in Explorer while recording. Expected: the `.csv` already exists there (the file is created in its final folder from the first sample; there is no temp file elsewhere).
5. Click STOP. Expected: the status line reads `Saved <n> samples -> <path>  .  log folder: C:\Users\ohio_\OneDrive\Personal\Vehicle\GTPro`; the Record tooltip reads `Last recording: <path>` then `Log folder: C:\Users\ohio_\OneDrive\Personal\Vehicle\GTPro`; the "Datalog saved" box offers to load it.
6. Look in GTPro. Expected: the recording's `.csv` and its `.meta.json` (same stem) are both there, and the `.meta.json` is non-empty valid JSON (open it in a text editor); a long-format sidecar beside them is expected only if the recorder writes one for this run. The file is not zero-length and the sample count in the status matches the CSV rows (+/- 1, as D-13).
7. Look in the default folder `Documents\OpenOBD\Logs`. Expected: NO new file from this run is there (the recording went only to GTPro).
8. Check the file in OneDrive (Explorer status icon, or onedrive.live.com). Expected: the files sync (green tick) with no "can't sync" error. Record the state: `________`.
9. Answer "Yes" in the "Datalog saved" box (or load it with Ctrl+L). Expected: the log opens in Log Analysis.

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### LF-10 After a reset, recordings return to the default folder and say nothing odd [GT] - P1
Setup: GT live; log folder reset to default (LF-07).
1. Record about 10 s and stop. Expected: the file and its `.meta.json` are in `Documents\OpenOBD\Logs` (created if it did not exist); no "Log folder unavailable" text in the status, tooltip or after Stop.
2. Expected: the post-Stop status still names the folder (`log folder: <default folder>`) and the tooltip reads `Log folder: <default folder>`.
3. Expected: nothing new in GTPro.

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### LF-11 Folder unavailable at record start: default is used and the UI says so [GT] - P0
Setup: GT live. Set the log folder to `$fx\lf\vol` through Settings (accepted: it is writable now). Then make it unusable WITHOUT changing the setting: with OpenOBD running, delete `$fx\lf\vol` and create a plain FILE with the same name (`Remove-Item -Recurse $fx\lf\vol; New-Item -ItemType File $fx\lf\vol`). (A removed USB drive path is an equivalent fixture; a merely deleted folder is NOT, because the recorder re-creates a missing folder.)
1. Start a recording. Expected: it starts (not refused), and the status line begins `Log folder unavailable: <fx>\lf\vol (using default)` then a dash and the reason (for example a FileExistsError text), then `Recording -> <default folder>\<file>.csv`. The wording must say the default is being used; a silent fallback is a Fail.
2. Hover the Record button. Expected: the tooltip's first line is the same `Log folder unavailable: ... (using default)` sentence (with the reason), second line `Recording to <default folder>\<file>.csv`.
3. Let it run 10 s and click STOP. Expected: the status line still begins with the `Log folder unavailable ... (using default)` sentence and then reads `Saved <n> samples -> <default path>  .  log folder: <default folder>`; the tooltip still carries the unavailable sentence plus `Last recording:` and `Log folder: <default folder>`.
4. Expected: the file and `.meta.json` are in the default folder and nowhere else; nothing was written to `$fx\lf\vol` or its parent; Preferences STILL shows `$fx\lf\vol` as the log folder (the setting is not silently rewritten or reset).
5. Restore the folder (delete the file, `New-Item -ItemType Directory "$fx\lf\vol"`) and record again. Expected: the recording goes to `$fx\lf\vol`, the unavailable sentence is gone from the status and the tooltip.
6. Repeat step 1 with `$fx\lf\readonly` made the log folder by editing the settings file (the chooser refuses it, LF-05) and the ACL deny applied. Expected: the same honest fallback (access-denied reason).
7. Clean up: restore ACLs, delete the fixture recordings, set the log folder as Ron wants it left.

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### LF-12 Load Log opens in the log folder - P1
Setup: GTPro holds at least one `.csv` (from LF-09; otherwise copy any recording into it). Log folder set to GTPro.
1. File -> Load Log... (Ctrl+L). Expected: the file chooser titled "Load log" opens inside `C:\Users\ohio_\OneDrive\Personal\Vehicle\GTPro` and the "Logs (*.csv)" filter is active.
2. Open the recording. Expected: it loads in Log Analysis with its `.meta.json` events (amber markers) found beside it.
3. Repeat from the Log Analysis toolbar Load button. Expected: the same start folder.
4. Reset the log folder to default (LF-07) and Load Log again. Expected: it starts in `Documents\OpenOBD\Logs` if that folder exists, else Qt's default (no error).
5. Set the log folder to `$fx\lf\vol`, delete `$fx\lf\vol`, then Load Log. Expected: no error; the chooser starts in the default folder (an absent configured folder is skipped).
6. Clean up: delete the test recordings from GTPro (Recycle Bin) unless Ron keeps them.

Pass: [ ]  Fail: [ ]  Notes: ____

### 7D.3 Live Data: Open session...

### LF-13 Open session... button, tooltip and recent menu [BOTH] - P1
Setup: clean state (no `livedata/recent_files`), data root `$fx\truck-mcp` with nothing or S-ARCH.
1. Open Live Data. Expected: the toolbar has an "Open session..." button (with its glyph) on the 36 px toolbar, with a small arrow beside it; Rescan, Fields..., Hold, the Window combo and the filter field are still all reachable.
2. Hover the button. Expected: a plain-text tooltip saying a truck-mcp session file can be opened from any folder, read-only, and that the arrow lists recent files. It must not claim the file is always shown as archived (an opened file's state comes from its data, LF-14 and LF-20). Record the exact wording: `________`.
3. Click the arrow with no recent files. Expected: one disabled entry "No recent session files".
4. At 1366x768, narrow the window to its minimum. Expected: the new button does not push the filter field or the picker off the toolbar (an overflow ">>" is acceptable); the page minimum width is not increased noticeably (measure and note it: `________`).
5. Check the dark palette at both resolutions. Expected: the button, its arrow and the menu use the dark palette; no white menu.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-14 Open a copied (ended) session file: picker entry, chips, note, as-of [BOTH] - P0
Setup: OS-ARCH in `$fx\picked` (outside the scanned root); the scanned root holds a different drive (S-ARCH or S-SIM) so the picker has its own entries. Compute the SHA-256 of OS-ARCH before opening: `________`. Newest sample timestamp of OS-ARCH (Preconditions): `________`.
1. Click "Open session...". Expected: a chooser titled "Open session" opens with the filter "truck-mcp session (*.tmsession.db)" selected and "All files (*)" available.
2. Choose `$fx\picked\os-arch.tmsession.db`. Expected: no popup; the session binds.
3. Look at the picker. Expected: the new entry is FIRST in the list, reads `file . os-arch.tmsession.db` (not the drive label), and is selected; the scanned drives follow it.
4. Read the chips. Expected: the normal chips come from the data: the drive has ended, so the first chip is the muted "archived"; then a neutral "opened file" on top of them; then the usual "<n> channels" and "<m> graphed" chips. No green "live" chip.
5. Read the note beside the breadcrumb. Expected: it begins with the FULL path `$fx\picked\os-arch.tmsession.db`, then the VIN (or "VIN unknown"), `source=<value>` and `as of <time>` where the time is the newest sample timestamp of the file (matches the value noted in Setup, in the same time zone the product shows), NOT the current clock time. Wait 60 s: Expected: the "as of" value does not change (an ended drive's newest sample does not move) while the wall clock does.
6. Look at every tile and the chart label. Expected: the archive palette, tile caption "archived drive", chart label "archived drive"; no tile reads "live".
7. Expected: no red banner. Hold is disabled or ignored because the drive has ended (LF-15).
8. Hover the picker entry. Expected: the tooltip shows the full path as plain text.
9. Choose a scanned drive from the picker. Expected: it binds with ITS own data-derived state (S-SIM shows the green "live" chip, S-ARCH "archived"), its "as of" is also the newest sample timestamp of THAT drive, and the "opened file" chip disappears; the `file . os-arch...` entry stays in the picker.
10. Select the `file . os-arch` entry again. Expected: it rebinds with the "opened file" chip.
11. Open OS-EMPTY (a session with no samples yet). Expected: the note reads `... as of no samples yet` (or the exact "no samples yet" text in the as-of position), never a clock time; tiles show no value; no crash and no banner. Select a scanned drive that also has no samples, if one is at hand, and Expected: the same wording for a discovered session.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-15 Hold: allowed while an opened file is live, ignored once it has ended - P1
Setup: OS-ARCH opened (ended drive); a truck-mcp simulated drive log running, its data root NOT scanned (as LF-20 Setup), its file opened directly; note the live file: `________`.
1. Select the opened OS-ARCH (ended). Expected: Hold is disabled (greyed), tooltip still reads as the Hold help text.
2. Press the Hold keyboard shortcut (see the tooltip) with focus on the page. Expected: nothing happens; no "held" chip appears.
3. Open the live file directly (LF-20 step 2). Expected: the chips are "live" and "opened file"; Hold is ENABLED (an opened live file is treated like a discovered live session).
4. Click Hold. Expected: a "held" chip is added (amber) next to "live" and "opened file"; the tiles and chart freeze at the held values; the Hold button shows its pressed state; the "as of" value freezes with them (it does not advance while held).
5. Wait 20 s while the logger keeps running. Expected: still frozen, still "held"; the logger's terminal shows its sample counter rising.
6. Click Hold again. Expected: the display catches up to the file's newest samples; the "held" chip disappears; the "as of" value jumps to the newest sample timestamp.
7. Hold again, then end the truck-mcp session normally from its terminal while held. Expected: within a few ticks the drive is seen as ended: the hold is released (Hold ignored once the drive has ended), the chip changes to "archived", Hold becomes disabled, and the "held" chip is gone. Record the time it took: `________`.
8. Select a scanned S-SIM drive: Hold is enabled and works as in LD-03/LD-12. Select the ended opened file again: disabled again.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-16 Opening the same file again re-selects it - P1
Setup: OS-ARCH opened, then a scanned drive picked.
1. Click "Open session..." and choose OS-ARCH again (or choose it from the arrow menu). Expected: no second entry; the picker still has exactly ONE `file . os-arch.tmsession.db`; it is selected and bound again; no banner.
2. Open it by a different spelling of the same path (e.g. `$fx\picked\..\picked\OS-ARCH.tmsession.db`, different case, or with forward slashes typed in the file-name box). Expected: still one entry (the same file is recognised case-insensitively).
3. Open OS-B. Expected: it becomes the FIRST entry; `file . os-arch...` is the second; the scanned drives follow.
4. Press Rescan (F5) and wait 6 s. Expected: the order is unchanged and the bound entry stays selected (no flicker back to the first scanned drive).

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-17 Recent files: newest first, no duplicates, capped at 8 - P1
Setup: clean `livedata/recent_files`; OS-ARCH and OS-B to OS-J (ten files).
1. Open OS-ARCH, then OS-B, OS-C, ... OS-J in that order (ten opens). Open the arrow menu. Expected: exactly 8 entries, newest first (OS-J ... OS-C); OS-ARCH and OS-B are gone; each entry reads `<file name>  -  <folder>` as plain text.
2. Open OS-D again from the menu. Expected: it moves to the top; still 8 entries, no duplicate.
3. Choose an entry from the arrow menu. Expected: it opens exactly as via the chooser (picker entry, chips, note); a click on the main button (not the arrow) opens the chooser starting in the folder of the newest recent file.
4. Check the settings file. Expected: `livedata/recent_files` lists the same 8 paths in the same order, and no other `livedata/` key changed.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-18 Recent list persists and prunes missing files at restart - P0
Setup: recent list as at the end of LF-17.
1. Quit and relaunch. Open the arrow menu. Expected: the same 8 entries in the same order.
2. Quit. Delete one file from the list (say the 3rd) in Explorer. Relaunch and open the arrow menu. Expected: the missing file is gone, 7 entries remain in order; the settings key now holds 7 paths (pruned at startup, not left to fail on click).
3. Quit; rename the folder `$fx\picked` to `$fx\picked-away` and relaunch. Expected: every entry is dropped ("No recent session files"); no error; rename the folder back.
4. Quit; edit the settings value to a single string (`livedata/recent_files=C:\...`) and, separately, to garbage (an integer). Relaunch each time. Expected: no crash; a one-entry value is read as one entry if the file exists; garbage reads as an empty list.
5. Expected: a previously opened file is NOT re-opened automatically at launch (Live Data starts on the scanned sessions); only the menu remembers it.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-19 A file that is not a session: danger banner, bound session kept - P0
Setup: S-ARCH (or an S-SIM) bound from the scanned root; OS-BAD (`not-a-session.tmsession.db`, really a CSV) in `$fx\picked`. Note the bound session label and recent list: `________`.
1. Click "Open session..." and choose OS-BAD. Expected: a red (danger) banner above the tiles reading `could not open not-a-session.tmsession.db: <reason>`; no popup dialog.
2. Expected: the session that was bound stays bound: same picker selection, same breadcrumb label and chips, tiles and chart still present, no "opened file" chip, no `file . not-a-session...` entry in the picker.
3. Wait 5 s (several ticks and one automatic rescan). Expected: the banner stays (a good poll of the bound session does not clear it); the live tiles continue.
4. Open the arrow menu. Expected: OS-BAD is NOT in the recent list.
5. Select another scanned session. Expected: the banner clears.
6. Repeat with an empty file (0 bytes `empty.tmsession.db`), a file of random bytes and a real SQLite database that is not a truck-mcp session (e.g. create one with `python -I -c "import sqlite3; sqlite3.connect(r'$fx\picked\other.tmsession.db').execute('create table t(x)')"`). Expected: each shows `could not open <name>: <reason>` and keeps the bound session; none crashes the page.
7. Click "Open session..." and press Cancel, then once more and close it with Esc. Expected: a no-op: no banner, picker and bound session unchanged, recent list unchanged.
8. Open OS-ARCH (valid) after a failed open. Expected: the banner clears and the file binds normally.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-20 A file still being written reads live, from its data, and never archived while it advances - P0
Setup: no GT needed. Start a simulated drive log in truck-mcp (`session_drive_log`; stay on the simulated source) with its data root NOT scanned by OpenOBD: let truck-mcp write to its normal root, while OpenOBD is launched with `TRUCK_MCP_DATA=$fx\truck-mcp`. Note the live file: `<logger root>\sessions\<name>.tmsession.db` (the logger root is `%LOCALAPPDATA%\Programs\truck-mcp-app` for the field kit): `________`. The logger keeps a terminal open; its sample counter must keep rising throughout.
1. (Optional, only if you can open the file within the first second of the drive log.) Open the live file immediately after it starts. Expected: the note reads `as of no samples yet`, then switches to a timestamp once the first sample lands, with no restart of the page.
2. Open the ORIGINAL live file directly with "Open session..." (read-only open, no locking). Expected: a picker entry `file . <name>.tmsession.db` first in the list; the first chip is the green "live" (the drive has not ended and its newest sample is advancing), followed by "SIMULATED" (red, source simulated) and the neutral "opened file" chip; it is NOT "archived" and NOT "not advancing". Every tile uses the live palette with caption "live"; the chart label reads "live". Hold is enabled (LF-15).
3. Read the note. Expected: it begins with the file's full path, then VIN, `source=simulated` and `as of <time>` where the time is the newest sample timestamp of the file and moves forward as samples arrive (about once per sample), not the time of the last poll. Compare with the logger terminal: the as-of time is within one sample interval of the logger's latest sample.
4. Watch for 60 s. Expected: tile values change with the file's newest samples; the chips NEVER read "archived" while the file advances; no banner appears.
5. Expected: the logger is not disturbed: its terminal shows no error and no "database is locked", its sample counter keeps rising, and the file keeps growing (check its size twice). Opening a file takes no lock and does not stop truck-mcp. A `-wal` / `-shm` pair appearing beside the file is allowed (see Connection semantics).
6. Copy the file mid-write to `$fx\picked\copy-of-live.tmsession.db` (with its `-wal` / `-shm` files if they exist) and open the copy. Expected: picker entry `file . copy-of-live.tmsession.db` with the "opened file" chip; the copy does not grow, so its state follows the data: it may read "live" for the first moments while its newest sample is recent, then turns to amber "not advancing" within about a minute (as LD-02), never "live" indefinitely; its "as of" stays fixed at the copy's last sample time and does not follow the wall clock. It must never read "archived" (the drive has not ended).
7. Kill the logger process abruptly (do not end the session) and wait about a minute. Expected: the opened original entry (bind it) turns amber "not advancing"; "as of" stays at the last sample time; "opened file" chip stays.
8. Stop OpenOBD. Relaunch it with `TRUCK_MCP_DATA` set to the LOGGER's data root (so the scan discovers the live file) and the logger running again on a new drive log. In the picker the live drive appears as a normal entry with the green "live" chip. Open the same file via "Open session...". Expected: no second `file .` entry is created; the picker re-selects the DISCOVERED entry, and Hold is enabled. Pass only if there is exactly one entry for that file.
9. End the truck-mcp session normally. Expected: the bound live entry (discovered or opened) turns to the muted "archived" within a few ticks, the "as of" value stays at the last sample, and Hold is disabled.
10. Clean up: delete the copies in `$fx\picked`, never touch the original file.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-21 Rescan keeps an opened entry while it is not discoverable; a discoverable one becomes a normal session - P1
Setup: OS-ARCH opened; two scanned sessions in `$fx\truck-mcp\sessions`.
1. Press Rescan (F5). Expected: the `file . os-arch...` entry is still FIRST and still selected; the bound session did not change; the "opened file" chip is still there.
2. Add a third drive to `$fx\truck-mcp\sessions` and Rescan. Expected: the picker is rebuilt with the new drive; the opened entry stays first and keeps its "opened file" chip.
3. Copy `os-arch.tmsession.db` into `$fx\truck-mcp\sessions` (it now becomes discoverable) with the opened entry still bound. Rescan. Expected: the separate `file . os-arch...` entry is dropped; the file appears as a normal discovered entry (labelled with the drive label, not `file .`); the "opened file" chip disappears and the session behaves as a discovered session (chips from the data, Hold per LF-15); exactly one entry for that drive.
4. Select a scanned drive, then delete `os-b.tmsession.db` (opened earlier) in Explorer (the file cannot be deleted while OpenOBD has it bound: select another session first; if Explorer still reports it in use, record that). Rescan. Expected: its `file .` entry disappears from the picker; no error banner; the recent menu still lists the path until the next restart prunes it (LF-18), and choosing it then shows `could not open ...` rather than crashing.
5. Delete the whole `$fx\truck-mcp` folder while the page is open with an opened file present. Expected: the opened entry remains and stays usable (the empty state "truck-mcp data root not found." appears only when nothing is opened as well); then Rescan with it removed: the empty state returns.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-22 Hostile characters in an opened file's path render literally - P1
Setup: OS-HOSTILE at `$fx\picked\a&lt;b &amp; 100%41\x.tmsession.db`.
1. Open it. Expected: the picker entry `file . x.tmsession.db`; the note starts with the full path shown literally (`&lt;b`, `&amp;`, `%41` as typed; no entity decoding, no bold, no underline).
2. Hover the picker entry and the note. Expected: the tooltips show the path literally.
3. Open the arrow menu. Expected: the entry is `x.tmsession.db  -  <folder>`; the folder text shows a single `&` where the name has `&` (an ampersand is not eaten as a keyboard mnemonic and no letter is underlined).
4. Try to open a path that contains `&` in a file name that is not a session (OS-BAD renamed `a&b.tmsession.db`). Expected: the banner reads `could not open a&b.tmsession.db: <reason>` literally.

Pass: [ ]  Fail: [ ]  Notes: ____

### LF-23 Opening never writes rows: the session file itself is byte-identical - P0
Setup: OS-ARCH and, for step 3, the live file of LF-20; Process Monitor optional.
1. Compute the SHA-256 of `os-arch.tmsession.db` ONLY and note the `$fx\picked` listing (names, sizes, timestamps). Open the file, let it tick for 30 s, select another session, quit OpenOBD.
2. Expected: the SHA-256 of `os-arch.tmsession.db` is identical to the value before, and its last-write time is unchanged. A `os-arch.tmsession.db-wal` and / or `-shm` file appearing beside it while it was open is ALLOWED (SQLite may create WAL sidecars; the file is opened without locking and OpenOBD never writes rows); record whether they appeared and whether they are gone after quit: `________`. Any other new file beside it is a Fail.
3. Repeat with a live file being written by truck-mcp (LF-20): compare nothing about the live file's hash (truck-mcp is appending to it). Instead check that truck-mcp is unaffected: no "database is locked", checkpoint or truncation error in the logger terminal, its sample counter keeps rising, and the file keeps growing. Then take a COPY of the file at a moment the logger has stopped (end the session), open the copy and repeat steps 1 and 2 on the copy.
4. With Process Monitor filtered on `.tmsession.db` (optional), Expected: OpenOBD performs no `WriteFile`, `SetEndOfFile`, `DeleteFile` or rename on the `.tmsession.db` itself; writes to `-wal` / `-shm` sidecars are allowed. Record N/A if Process Monitor is not available.
5. Check the settings file after the run. Expected: the keys written by this section are only `logs/dir` and `livedata/recent_files` (plus the existing `livedata/` layout keys from using the page); opening a file wrote nothing into the session file or the scanned root.

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

### LF-24 The log folder must be an absolute path; relative values are refused and the default is used [GT] - P0
Setup: OpenOBD closed. The settings file is edited by hand (the chooser can only produce absolute paths). The setting expands environment variables, then must be absolute. GT live for steps 5 to 7; steps 1 to 4 need no GT.
1. Set `logs/dir` to the relative value `GTPro` and launch. Open Preferences. Expected: the row does not present `GTPro` as a working folder; it reads the default (or states that the value is refused). Record exactly what it shows: `________`. No folder named `GTPro` is created in the working directory of the launch or anywhere else.
2. Repeat with `.\logs`, `..\logs` and `logs\sub`. Expected: each is refused the same way; no folder is created relative to the current directory.
3. Open Settings -> Log folder... and cancel. Expected: a relative value already in the settings is not silently "fixed" by Cancel (the key is unchanged).
4. Environment variables: set `logs/dir` to `%LOCALAPPDATA%\OpenOBD-lf-test` (a variable that is defined) and then to `%NO_SUCH_VAR_LF%\logs` (undefined, stays relative after expansion). Expected: the defined variable is expanded and the result, being absolute, is the folder used (Preferences shows the expanded path or the setting; record which: `________`); the undefined variable leaves a relative text and is refused like step 1. Record any difference from this reading of the fix round (the fix round states the value "must be absolute"; if the build refuses an env-var value even when it expands to an absolute path, record that and flag it): `________`.
5. [GT] With a refused value set (`GTPro`), start a recording. Expected: it starts in the DEFAULT folder, and the status line begins `Log folder unavailable: GTPro (using default)` followed by a dash and the reason `Log folder must be an absolute path`, then `Recording -> <default folder>\<file>.csv`. A silent fallback is a Fail.
6. [GT] Hover the Record button. Expected: the tooltip's first line carries the same sentence with the reason `Log folder must be an absolute path`.
7. [GT] Stop the recording. Expected: the status still starts with the `Log folder unavailable: GTPro (using default)` sentence followed by `Saved <n> samples -> <default path>  .  log folder: <default folder>`; the file and `.meta.json` are in the default folder only; nothing was written to a relative `GTPro` folder; Preferences still shows the stored value (not silently rewritten).
8. Clean up: delete the key (or Reset log folder to default) and delete the fixture recordings.

Pass: [ ]  Fail: [ ]  N/A: [ ]  Notes: ____

---

**Run log addendum (Log folder and Open session)**: add the 12 P0 cases of section 7D (LF-02, LF-05, LF-06, LF-07, LF-09, LF-11, LF-14, LF-18, LF-19, LF-20, LF-23, LF-24) to the P0 pass count (after 7A, 7B and 7C the total is 98; with 7D it becomes 110) and record LF-nn ids for any defects. LF-09, LF-11 and LF-24 (steps 5 to 7) need the GT: if they cannot be run on the bench day, record N/A with the reason (covered by `test_setting_is_honoured_and_named_in_tooltip_and_status` and `test_unwritable_setting_falls_back_honestly`, plus the absolute-path test added in the fix round) and carry them to the first truck session; the other nine P0 cases never need the GT.

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
1. Help -> About. Expected: "OpenOBD 0.39.0", vehicle and controller info, and the safety text.

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
| P0 pass count | ___ / 110 |
| Defects raised (ids) | |
