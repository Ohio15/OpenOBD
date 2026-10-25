@echo off
REM Build dist\openobd.exe from this checkout with PyInstaller (openobd.spec).
REM Works from any checkout location: everything is resolved relative to this
REM script's directory, using the checkout's own .venv. Output goes to
REM build.log. After the build, the exe's Windows version resource is checked
REM against openobd\__init__.py __version__; a mismatch fails the build.
REM Exit code: 0 = built and version verified, non-zero = failed (see build.log).
REM Full release procedure: docs\RELEASE.md
setlocal
cd /d "%~dp0"
set "PYTHONHOME="
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo No virtualenv at "%PY%".
    echo Create it first: py -3 -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 2
)

"%PY%" -m PyInstaller openobd.spec --noconfirm --clean > build.log 2>&1
set "RC=%ERRORLEVEL%"
echo BUILD_DONE_EXIT_%RC% >> build.log
if not "%RC%"=="0" (
    echo BUILD FAILED ^(exit %RC%^) - see build.log
    exit /b %RC%
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$m = Select-String -Path 'openobd\__init__.py' -Pattern '__version__\s*=\s*\"(\d+\.\d+\.\d+)\"';" ^
  "if (-not $m) { Write-Host 'cannot read __version__ from openobd\__init__.py'; exit 3 }" ^
  "$want = $m.Matches[0].Groups[1].Value;" ^
  "$got = (Get-Item 'dist\openobd.exe').VersionInfo.ProductVersion;" ^
  "if ($got -ne $want) { Write-Host \"VERSION MISMATCH: exe=$got package=$want\"; exit 4 }" ^
  "Write-Host \"built dist\openobd.exe version $got\""
set "RC=%ERRORLEVEL%"
echo VERIFY_DONE_EXIT_%RC% >> build.log
exit /b %RC%
