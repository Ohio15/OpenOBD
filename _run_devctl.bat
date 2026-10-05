@echo off
REM GATED DEVICE CONTROL -- this MOVES hardware. Ron-run only.
REM Lists the controls captured on this truck, asks which one to run, then:
REM reads engine running + vehicle stopped from the ECM, asks you to type the
REM exact confirmation (transmission in NEUTRAL, foot on the brake), replays
REM the captured request byte for byte, streams the module's data, and ALWAYS
REM hands control back to the module ($20) at the end. Ctrl+C aborts safely.
REM Close OpenOBD / Techline / the Autel session first.
setlocal
cd /d D:\Projects\OpenOBD
set "PYTHONHOME="
set "PYTHONUNBUFFERED=1"
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.devctl --list
set "CID=%~1"
if "%CID%"=="" set /p "CID=Number of the control to run (blank = cancel): "
if "%CID%"=="" (echo Cancelled - nothing sent. & pause & exit /b 0)
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.devctl --run %CID%
pause
