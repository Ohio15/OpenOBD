@echo off
REM LISTEN-ONLY recording of HS-GMLAN diagnostic traffic through the GT while
REM ANOTHER scan tool (the Autel, on the Y-splitter) runs a function.
REM 1. Close OpenOBD and Techline. 2. Double-click this, enter a label
REM (e.g. tccm-learn). 3. When it says LISTENING, run the function on the
REM Autel. 4. When the Autel finishes, press Ctrl+C here (answer N if cmd asks
REM to terminate the batch job, so the summary prints). Records up to 10 min
REM to canrec-<label>.jsonl. Never transmits. Ron-run (opens the GT).
setlocal
set "LABEL=%~1"
if "%LABEL%"=="" set /p "LABEL=Label for this recording (e.g. tccm-learn): "
if "%LABEL%"=="" (echo No label entered - nothing recorded. & pause & exit /b 2)
cd /d D:\Projects\OpenOBD
set "PYTHONHOME="
set "PYTHONUNBUFFERED=1"
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.canrec %LABEL%
pause
