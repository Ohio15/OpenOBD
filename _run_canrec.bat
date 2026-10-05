@echo off
REM LISTEN-ONLY recording of diagnostic traffic through the GT while ANOTHER
REM scan tool (the Autel, on the Y-splitter) runs ONE function. Never transmits.
REM 1. Close OpenOBD and Techline.  2. Double-click this.  3. Pick the bus:
REM      M = MAIN bus  (engine, transmission, transfer case, ABS, body module)
REM      B = BODY bus  (doors, HVAC, instrument cluster, radio -- single-wire)
REM The file is named automatically: canrec-main-<date>-<time>.jsonl or
REM canrec-body-<date>-<time>.jsonl.  4. When it says LISTENING, run the
REM function on the Autel.  5. Ctrl+C here when the Autel is done (answer N if
REM cmd asks to terminate the batch job).  Ron-run (opens the GT).
setlocal
set "BUS=%~1"
if "%BUS%"=="" set /p "BUS=Which bus? M = main (engine/trans/4WD/ABS), B = body (doors/HVAC/cluster/radio): "
if /i "%BUS%"=="M" set "BUS=hs"
if /i "%BUS%"=="B" set "BUS=sw"
if /i not "%BUS%"=="hs" if /i not "%BUS%"=="sw" (echo Enter M or B. Nothing recorded. & pause & exit /b 2)
cd /d D:\Projects\OpenOBD
set "PYTHONHOME="
set "PYTHONUNBUFFERED=1"
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.canrec --bus %BUS%
pause
