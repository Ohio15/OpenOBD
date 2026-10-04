@echo off
REM Headless per-module DTC scan over the OBDX Pro GT. Writes dtcscan.out and
REM prints it. The GT must be free (close the OpenOBD GUI first) and out of
REM binary mode. Ron-run (opens the GT).
cd /d D:\Projects\OpenOBD
set "PYTHONHOME="
set "PYTHONUNBUFFERED=1"
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.dtcscan > dtcscan.out 2>&1
type dtcscan.out
