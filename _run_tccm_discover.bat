@echo off
REM READ-ONLY discovery of the transfer case module's data identifiers (7E4)
REM over the GT's pass-thru driver. Ron-run (opens the GT). Engine RUNNING or a
REM charger on: a full sweep takes up to about an hour with the key on.
REM Hits are saved as they arrive to didscan-7E4-supported.jsonl; if it is
REM interrupted, resume with:  _run_tccm_discover.bat --start XXXX
REM (XXXX = the last "progress" value in that file).
cd /d D:\Projects\OpenOBD
set "PYTHONHOME="
set "PYTHONUNBUFFERED=1"
echo Discovering (read-only). Progress is written to didscan-discover.out and
echo didscan-7E4-supported.jsonl as it goes; this window stays quiet until done.
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.didscan --module 7E4 --discover %* > didscan-discover.out 2>&1
type didscan-discover.out
pause
