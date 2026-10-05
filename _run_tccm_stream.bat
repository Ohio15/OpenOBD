@echo off
REM Stream the transfer case module's position pair + knob + status, sampled
REM TOGETHER by the module ($2C/$AA), for 30 s to tccm-stream-<label>.csv.
REM Non-actuating. Double-click, enter a label, TURN THE KNOB when told.
REM Engine running, transmission in Neutral, foot on the brake. Ron-run.
setlocal
set "LABEL=%~1"
if "%LABEL%"=="" set /p "LABEL=Label for this stream (e.g. 2hi-auto-4hi): "
if "%LABEL%"=="" (echo No label entered - nothing recorded. & pause & exit /b 2)
cd /d D:\Projects\OpenOBD
set "PYTHONHOME="
set "PYTHONUNBUFFERED=1"
"D:\Projects\OpenOBD\.venv\Scripts\python.exe" -m openobd.tccmprobe --stream %LABEL% --seconds 30
pause
