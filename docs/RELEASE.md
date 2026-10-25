# Releasing OpenOBD (Windows exe)

This is the whole release path: test, build `dist\openobd.exe`, check its
version, install it to `%LOCALAPPDATA%\Programs\OpenOBD\OpenOBD.exe` (where
the Start menu shortcut points), check the installed copy, and roll back if
needed. Every step runs on the Windows box from a checkout of this repo. No
other tooling is required.

Commands are PowerShell, run from the repo root (the folder holding
`openobd.spec`). The checkout can live anywhere: `_build.bat` and
`_deploy.ps1` resolve paths from their own location.

## 1. Prerequisites (once per checkout)

- Windows with Python 3 (the `py` launcher) installed.
- A virtualenv named `.venv` in the repo root with the requirements
  installed. `_build.bat` uses `.venv\Scripts\python.exe` and refuses to run
  without it.

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` holds both runtime (PySide6, pyserial, pyqtgraph, numpy)
and build/test (pytest, pyinstaller) dependencies.

## 2. Pick what to release

Release from an up-to-date `main` whose top `CHANGELOG.md` entry is the
version being shipped:

```powershell
git fetch origin
git switch main
git pull --ff-only
Select-String -Path openobd\__init__.py -Pattern '__version__'
```

`tests/test_version.py` fails if the top `## X.Y.Z` heading in
`CHANGELOG.md` differs from `__version__`, so step 3 catches a missed bump.

## 3. Test gate

Both must pass. Do not build on a red run.

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q
$env:QT_QPA_PLATFORM = "offscreen"; .venv\Scripts\python.exe tests\smoke_gui.py; Remove-Item Env:QT_QPA_PLATFORM
```

The pytest suite is headless (no Qt, no hardware). `tests\smoke_gui.py`
builds the real main window offscreen; it isolates its own settings and
truck-mcp data root, so it does not touch your real settings.

## 4. Build

```powershell
.\_build.bat
echo "exit $LASTEXITCODE"
```

What it does:

1. Runs `.venv\Scripts\python.exe -m PyInstaller openobd.spec --noconfirm --clean`,
   writing all output to `build.log` (`BUILD_DONE_EXIT_<n>` is appended).
2. On success, compares the exe's Windows version resource with
   `__version__` in `openobd\__init__.py` (`VERIFY_DONE_EXIT_<n>` is
   appended to `build.log`).

Exit codes: `0` built and version matches; `2` no `.venv`; `3` cannot read
`__version__`; `4` version mismatch; anything else is the PyInstaller exit
code (read `build.log`).

The spec builds one windowed file, `dist\openobd.exe`, with the seed
calibration and icon bundled. Its version resource comes from
`openobd\__init__.py` at build time, so it cannot drift from the package
version.

## 5. Check the built exe

`_build.bat` already does this. To check it yourself:

```powershell
(Get-Item dist\openobd.exe).VersionInfo.ProductVersion
Select-String -Path openobd\__init__.py -Pattern '__version__'
```

The two versions must be equal. Optionally launch `dist\openobd.exe` once
and confirm it opens on the 2010 Silverado calibration.

## 6. Back up the installed copy

`_deploy.ps1` overwrites the installed exe and keeps no copy. Save the
current one first so step 9 has something to restore:

```powershell
$dir = Join-Path $env:LOCALAPPDATA "Programs\OpenOBD"
$cur = Join-Path $dir "OpenOBD.exe"
if (Test-Path $cur) {
    $v = (Get-Item $cur).VersionInfo.ProductVersion
    if (-not $v) { $v = "unknown-" + (Get-Item $cur).LastWriteTime.ToString("yyyyMMdd-HHmmss") }
    New-Item -ItemType Directory -Force (Join-Path $dir "backup") | Out-Null
    Copy-Item $cur (Join-Path $dir "backup\OpenOBD-$v.exe") -Force
}
```

## 7. Deploy

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\_deploy.ps1
```

What it does:

- Fails with "no build at ... run _build.bat first" if
  `dist\openobd.exe` is missing.
- Creates `%LOCALAPPDATA%\Programs\OpenOBD` if needed.
- If OpenOBD is running from that folder, it asks each window to close.
  Unsaved calibration edits bring up the "Save before closing?" prompt. It
  waits up to 60 seconds. If anything is still open, it stops with
  "deploy ABORTED" and copies nothing. It never force-kills the app and
  never installs beside a running copy. Answer the prompt (or close the app)
  and run it again.
- Copies `dist\openobd.exe` over `OpenOBD.exe` and prints the file time.

## 8. Check the installed copy

```powershell
(Get-Item "$env:LOCALAPPDATA\Programs\OpenOBD\OpenOBD.exe").VersionInfo.ProductVersion
```

This must match the version from step 5. Then start OpenOBD from the Start
menu (the shortcut points at this file) and confirm it opens.

## 9. Roll back

Restore the copy saved in step 6, using the same close-first rule as
`_deploy.ps1` (close OpenOBD before copying; never copy over a running
copy):

```powershell
$dir = Join-Path $env:LOCALAPPDATA "Programs\OpenOBD"
Get-ChildItem (Join-Path $dir "backup")          # pick the version to restore
if (Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$dir\*" }) {
    throw "OpenOBD is running: close it first"
}
Copy-Item (Join-Path $dir "backup\OpenOBD-<version>.exe") (Join-Path $dir "OpenOBD.exe") -Force
(Get-Item (Join-Path $dir "OpenOBD.exe")).VersionInfo.ProductVersion
```

With no backup, rebuild the earlier version from git. Release commits carry
the version in their subject, e.g. `feat(devctl): ... (v0.31.0)`:

```powershell
git log --oneline --grep "(v0\.30\.1)"        # find the release commit
git switch --detach <commit>
.\_build.bat                                    # then steps 6-8
git switch main
```

Exes built before v0.18.0 have no version resource. For those, use the
file time (`(Get-Item <exe>).LastWriteTime`) to tell versions apart.
