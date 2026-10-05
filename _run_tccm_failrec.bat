@echo off
REM Read the transfer case module's stored FAILURE RECORDS ($12): the data it
REM froze at the moment it set a code (C0398). Non-actuating. Key on (engine
REM may run). Close OpenOBD / Techline / other tools first. Ron-run.
cd /d D:\Projects\OpenOBD
set "PYTHONHOME="
set "PYTHONUNBUFFERED=1"
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.tccmprobe --failure-records
pause
