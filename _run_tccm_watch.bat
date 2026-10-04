@echo off
REM READ-ONLY live recording of the transfer case position, knob and status
REM identifiers (~5 samples/s) while you turn the knob. Double-click it, enter
REM a label (e.g. AUTO-to-4HI), then TURN THE KNOB as soon as it says
REM "TURN THE KNOB NOW". Records 25 s to didscan-7E4-watch-<LABEL>.csv.
REM Engine running. Ron-run (opens the GT).
setlocal
set "LABEL=%~1"
if "%LABEL%"=="" set /p "LABEL=Label for this recording (e.g. AUTO-to-4HI): "
if "%LABEL%"=="" (echo No label entered - nothing recorded. & pause & exit /b 2)
cd /d D:\Projects\OpenOBD
set "PYTHONHOME="
set "PYTHONUNBUFFERED=1"
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.didscan --module 7E4 --watch %LABEL% --seconds 25
pause
