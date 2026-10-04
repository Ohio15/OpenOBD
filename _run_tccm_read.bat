@echo off
REM READ-ONLY snapshot of every transfer case identifier found by discovery,
REM labelled with the vehicle state. Double-click it: it asks for the knob
REM position (2HI, AUTO, 4HI...). Or run:  _run_tccm_read.bat 2HI
REM Writes didscan-7E4-<LABEL>.json. Ron-run (opens the GT).
setlocal
set "LABEL=%~1"
if "%LABEL%"=="" set /p "LABEL=Knob position now (2HI, AUTO, 4HI, 4LO, N): "
if "%LABEL%"=="" (echo No position entered - nothing read. & pause & exit /b 2)
cd /d D:\Projects\OpenOBD
set "PYTHONHOME="
set "PYTHONUNBUFFERED=1"
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.didscan --module 7E4 --read %LABEL% > didscan-read-%LABEL%.out 2>&1
type didscan-read-%LABEL%.out
pause
