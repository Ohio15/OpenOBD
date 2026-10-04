@echo off
REM READ-ONLY snapshot of every transfer case identifier found by discovery,
REM labelled with the vehicle state. Usage:  _run_tccm_read.bat 2HI
REM Run once per knob position (2HI, AUTO, 4HI) after each shift completes.
REM Writes didscan-7E4-<LABEL>.json. Ron-run (opens the GT).
if "%~1"=="" (echo usage: _run_tccm_read.bat LABEL  e.g. 2HI & exit /b 2)
cd /d D:\Projects\OpenOBD
set "PYTHONHOME="
set "PYTHONUNBUFFERED=1"
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.didscan --module 7E4 --read %1 > didscan-read-%1.out 2>&1
type didscan-read-%1.out
