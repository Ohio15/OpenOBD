# OpenOBD — calibration viewer/editor + log overlay for truck-mcp

An HP-Tuners-style desktop app for the **2010 Silverado 1500 5.3 (E38 ECM /
T43 TCM, 6L80)** calibration work. It renders the harvested calibration values
as editable, heatmapped tables; overlays captured logs onto tables; analyzes a
log's trims / knock / shift points; and drives a live gauge dashboard.

It is the **UI layer of the truck-mcp own-app suite** — it reads the same
calibration model the `wot_analyzer` / `hpl_bridge` modules produce, and its
dashboard reads from the same transport seam `gt.py` (the OBDX Pro GT port)
will implement. No vendor software, no subscriptions, no `.hpt` encryption.

## What it is (and isn't)

* **Is:** a viewer/editor over *our own* calibration format (`.cal.json`) —
  values transcribed byte-precisely from the `#24` change sheet (both stock and
  new), plus a log-analysis engine that codifies the 8.8.26 baseline logic.
* **Is:** editable — cells save to `.cal.json` / export to CSV, so you can plan
  changes visually now.
* **Is NOT:** a flasher. It never writes an encrypted `.hpt`. Turning a
  `.cal.json` back into a flashable tune stays behind the gated Phase 3/4 path
  in the GT swivel spec. The tables here are for *planning and analysis*.

## Install & run (dev)

```bash
cd OpenOBD
python -m pip install -r requirements.txt
python -m openobd            # opens on the seeded 2010 Silverado #24 cal
# or: python run.py path/to/other.cal.json
```

Regenerate the seed calibration from the change-sheet source:

```bash
python -m openobd.seed_2010_silverado    # -> data/2010_silverado_24.cal.json
```

Run the tests:

```bash
python -m pytest tests/ -q                # headless core (calspec + logbin)
QT_QPA_PLATFORM=offscreen python tests/smoke_gui.py   # GUI smoke
```

## Build the Windows exe

On the Windows box, from the repo root:

```bash
python -m pip install -r requirements.txt
pyinstaller openobd.spec
# -> dist/openobd.exe   (single file, windowed, seed bundled)
```

The exe carries a Windows version resource generated from
`openobd/__init__.py`, so an installed copy can be checked without launching
it: `(Get-Item "$env:LOCALAPPDATA\Programs\OpenOBD\OpenOBD.exe").VersionInfo.ProductVersion`.
Release history: [CHANGELOG.md](CHANGELOG.md).

## The workspaces

Five top-level workspaces separate the workflows. **Dashboard** opens first;
**Tuning** holds monitoring/scanning/tuning (Editor, Scalars, Log Analysis
sub-tabs plus the table tree); **Diagnostics** holds troubleshooting and code
clearing; **Live Data** and **Active Tests** read truck-mcp's drive-log store
off disk (see *Reading truck-mcp drive logs* below):

* **Module Map** — the vehicle network drawn live: PC → OBDX GT → DLC, then
  the HS-GMLAN (500k) and SW-GMLAN (33.3k) bus rails with every installed
  module. *Scan Modules* walks the comms pipeline in stages (serial link →
  interface → DLC battery voltage → HS broadcast → per-module TesterPresent
  ping) and paints the verdict onto the map, so a failure shows **at the
  segment where it occurred** — tool link, DLC power, whole-bus, or a single
  silent module. SW-GMLAN modules are amber "unreachable via this path"
  (the ELM HS-CAN path can't open the single-wire bus), which is distinct
  from red "expected but silent". Red is only ever painted after a physical
  ping whose header and receive filter the GT confirmed; if the GT refuses
  the addressing commands the module is grey "not examined", and absence
  from the functional broadcast alone never counts as silence. A GT left in
  its binary J2534 mode by another tool is reported as such (unplug it from
  USB and the OBD port for 10 s), and an unparseable ATRV is "voltage
  unreadable", not a power fault. Addresses are the ones measured on this
  truck: ECM 7E0/7E8, TCM 7E2/7EA, EBCM 243/643, BCM 241/641 (HS; also the
  SW gateway). Click any module for its role and address.
* **Codes & Readiness** — stored / pending / permanent DTCs (modes 03/07/0A)
  with decoded SAE codes and common-code descriptions, MIL state and count,
  readiness monitor table, and *Clear Codes* behind a confirmation that
  spells out the readiness-monitor reset.

Diagnostics shares the dashboard's live GT connection (gauge polling pauses
around each diagnostic exchange) or opens its own link if nothing is
connected.

## Reading truck-mcp drive logs

**Live Data** and **Active Tests** read what [truck-mcp](../truck-mcp) records —
they never open the serial port, so they cannot contend with a running logger
for the adapter. truck-mcp writes each drive as one SQLite file
(`sessions/*.tmsession.db`) in WAL mode: *the database is the channel*, so a
drive this box never started, a drive a logger is still writing, and a drive
that finished last week all read identically. OpenOBD finds the store via
`$TRUCK_MCP_DATA`, else `D:/Projects/truck-mcp`, else `~/truck-mcp`.

* **Live Data** — pick a session; a tile grid shows the latest value per
  channel, polled once a second straight off the file. Every tile renders
  through the same five-state rule truck-mcp's own web UI enforces: **fresh**
  (measured now), **carried** (measured earlier and carried forward, with its
  age), **module error**, **no data** (answered with nothing), and **not read**
  (nobody asked) are five different facts and get five different tiles. A
  carried value never renders as a live one — the tool must not lie about the
  truck. The connection is opened `PRAGMA query_only`, so it can recover a WAL
  left by a crashed logger yet is physically unable to write.
* **Active Tests** — display-only vehicle-control state. truck-mcp's registry of
  executable controls is empty by policy (a supported CPID executes on first
  contact with whatever bytes were guessed, so none may be scanned for), and
  this page states that as a decision, not a blank. It reads truck-mcp's control
  journal and surfaces the one thing that matters most: whether a control
  session ended **without releasing an actuator** (an outstanding activation),
  shown as a loud warning. **OpenOBD holds no lease, owns no console, and sends
  no frame** — arm/fire live in truck-mcp's CLI behind an out-of-band console
  nonce, and wiring any active-test control here is gated on a hardware CPID
  probe session plus a per-exposure adversarial review.

## The tuning tabs

**Editor** — pick a table from the left tree (filter box narrows the 251-table
list); edit heatmapped cells in place. Changed-vs-stock cells are bold, each
cell's tooltip shows stock + Δ, and **Next Δ** cycles through changed cells.
Full editing suite: **undo/redo** (Ctrl+Z/Y), selection **math**
(set / + / − / × / %-scale with an Amount field; +/- keys nudge), linear
**interpolation** (↔ / ↕ / 2-D bilinear), **copy/paste as TSV** (round-trips
through Excel), and revert selection / whole table to stock — all undoable.
The **overlay** dropdown drops a loaded log onto the grid (operation-count
histogram, or mean of a chosen value channel). *Overlays are meant for
RPM×load breakpoint tables* (VE, spark) — for the shift **setpoint** tables the
meaningful log view is the observed-shift report (below), not a cell overlay.

**Scalars** — final drive ratios, tire circumferences, DoD flag: editable, with
stock + Δ and the HPT parameter-ID breadcrumb.

**Log Analysis** — load a VCM Scanner CSV export (Log File → Export Log File →
CSV) or a plain CSV. Stacked **time-series charts** (pyqtgraph) with a synced
crosshair cursor and per-channel readout; pick channels from the checklist,
with knock (red) and shift (cyan) **event markers** on every pane. Below the
charts, the text report: regime trims (idle/cruise/PE), knock events (real KR
channel, or a timing-collapse heuristic when KR isn't logged), fuel-pressure
sag, and **observed shift points** (from a `gear` channel, or inferred from
RPM drops) so you can compare real WOT shifts to the setpoint tables.

**Dashboard** — a gauge cluster with rolling sparklines and min/max capture
(click a gauge to reset). Replay has full transport controls — pause/resume,
speed (0.5–8×), and a seek slider. **⏺ Record** streams every sample losslessly
to disk (drain-based, not UI-tick-sampled) and offers the saved CSV straight
back to Log Analysis. Fed by a `LogReplaySource` or live by `GtDataSource`
(OBDX Pro GT), both behind the same `DataSource` seam.

## Architecture

```
openobd/
  calspec.py     data model: Calibration / Table / Axis / Scalar + .cal.json I/O   [stdlib only]
  logbin.py      log parse (VCM Scanner + plain CSV), channel mapping,
                 table binning (overlay), regime/knock analysis, shift detection   [stdlib only]
  editops.py     selection math / interpolation / TSV clipboard as pure
                 change-map planners (the GUI wraps them in undo commands)         [stdlib only]
  transport.py   DataSource seam: LogReplaySource now, GtDataSource stub for gt.py [stdlib only]
  tmstore.py     read truck-mcp *.tmsession.db drive logs; five-state display rule [stdlib only]
  ctljournal.py  read truck-mcp's control journal (outstanding activations)        [stdlib only]
  seed_2010_silverado.py   builds the #24 seed calibration from the change sheet
  model.py       Qt table model + heatmap delegate                                 [PySide6]
  livedata.py    Live Data + Active Tests workspaces over tmstore/ctljournal       [PySide6]
  app.py         main window / tabs / file ops                                     [PySide6]
data/            the seed .cal.json
tests/           headless unit tests + offscreen GUI smoke
```

The `calspec` / `logbin` / `transport` core is **pure stdlib** — importable by
truck-mcp's CLI tools and fully unit-tested without Qt. Only `model.py` /
`app.py` pull in PySide6.

## How this folds into truck-mcp

* `logbin.analyze_log` / `detect_shift_points` / `bin_log_to_table` are the
  concrete `wot_analyzer` the GT spec calls for — reuse them from the CLI.
* `transport.DataSource` is the dashboard side of the `ElmTransport` seam;
  `gt.py`'s live source implements `channels()/latest()/start()/stop()`.
* `.cal.json` is the interchange format between this editor and any future
  read/flash path — a Phase 3 calibration read populates the *real* breakpoint
  tables (VE, spark, torque) into the same model, and the overlays light up.

## Coalescing references into a best cal

`openobd.coalesce` merges N reference calibrations into one best cal with
per-parameter provenance — which source won, which rule picked it, and what
every other reference said (per-cell delta summaries for tables). Parameters
join across references by HPT ParameterID (display names differ between
harvests), by name otherwise. Tables merge whole-table — cells are never
mixed across sources inside one physical table. Pins are explicit verified-
hardware overrides that outrank every reference.

```bash
python -m openobd.coalesce \
  --ref stock=stock-of:data/2010_silverado_full.cal.json \
  --ref sheet24=data/2010_silverado_24.cal.json \
  --ref tune24=data/2010_silverado_full.cal.json \
  --policy data/policy_tow_first.json \
  --out data/2010_silverado_best.cal.json \
  --compare-out data/best_compare_report.json
```

`data/policy_tow_first.json` is the tow-first policy (stock authority,
hardware pins, #24's verified WOT rescale); the community tow consensus it
encodes lives with sources in `data/refs/tow_consensus_2010_silverado.md`.
The merged file is a normal `.cal.json` — open it in the app like any other.

## Provenance

Seed values transcribed from `24-claudes-edit-change-sheet.md`, verified in VCM
Editor via Compare → Comparison Log against `DO NOT CHANGE - 2010 Silverado
Stock - 10.13.24.hpt`. Vehicle: VIN 3GCRKTE35AG150432, E38 OS 12636005 / T43 OS
24254909, 4.11 gears, 35" tires, AFM disabled, E10 fuel.
