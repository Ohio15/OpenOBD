# Releasing OpenOBD (Windows exe)

This is the whole release path: test, build `dist\openobd.exe`, check its
version, install it to `%LOCALAPPDATA%\Programs\OpenOBD\OpenOBD.exe` (where
the Start menu shortcut points), check the installed copy, and roll back if
needed. Every step runs on the Windows box from a checkout of this repo. No
other tooling is required.

Commands are PowerShell, run from the repo root (the folder holding
`openobd.spec`). The checkout can live anywhere: `_build.bat` and
`_deploy.ps1` resolve paths from their own location.

**Run `_build.bat` and `_deploy.ps1` from PowerShell or cmd, never through Git
Bash/MSYS.** From Git Bash, `cmd /c "D:\...\_build.bat"` has its path argument
mangled: cmd prints its banner, exits 0, and the build never runs. That
silently left a stale `dist\openobd.exe` on the v0.34.0 release (discovery
455efc83). The exact lines (from the repo root, in PowerShell):

```powershell
.\_build.bat; "build exit $LASTEXITCODE"
powershell -NoProfile -ExecutionPolicy Bypass -File .\_deploy.ps1; "deploy exit $LASTEXITCODE"
```

Defence in depth: `_build.bat` deletes the previous `dist\openobd.exe` and
`build.log` before building, and `_deploy.ps1` refuses a build that does not
match the package (step 5a, step 7), so a skipped build now fails loudly
instead of deploying old code.

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

The same two commands run in CI on every PR and every push to main
(`.github/workflows/tests.yml`, Windows and Ubuntu); a green PR is not a
substitute for running them here against the tree you are about to build.

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

### 5a. Pre-deploy version check (explicit, do not skip)

Confirm the exe you are about to install is the one just built from this
tree, and that `build.log` shows a clean build and verify:

```powershell
(Get-Item dist\openobd.exe).VersionInfo.ProductVersion
Select-String -Path openobd\__init__.py -Pattern '__version__'
(Get-Item dist\openobd.exe).LastWriteTime        # must be from this build
Select-String -Path build.log -Pattern '^(BUILD|VERIFY)_DONE_EXIT_'   # both _0
```

`_deploy.ps1` enforces this itself and refuses (non-zero exit, nothing
copied, no backup taken, install dir untouched) unless all hold: last
`BUILD_DONE_EXIT_0` then `VERIFY_DONE_EXIT_0` in `build.log`; exe
`ProductVersion` equals `__version__`; no file under `openobd\` newer than the
exe; exe and `build.log` written within 10 minutes of each other (the exe is
written before the log's DONE lines, so "same build" is an mtime pairing, not
"exe newer than log"). If it refuses, run `_build.bat` from PowerShell and
retry. Do not work around the gate by copying the exe by hand.

## 6. Back up the installed copy

Automatic: `_deploy.ps1` copies the outgoing exe to
`%LOCALAPPDATA%\Programs\OpenOBD\backup\OpenOBD-<version>.exe` before it
overwrites it (an exe with no version resource is named
`OpenOBD-unknown-<file time>.exe`). Nothing to do here.

## 7. Deploy

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\_deploy.ps1
```

What it does:

- Runs the pre-deploy gate (step 5a) first. Any failure prints
  "deploy REFUSED (nothing copied, no backup taken): <reason>" and exits
  non-zero; this includes "no build at ... run _build.bat first" when
  `dist\openobd.exe` is missing.
- Test or dry-run against a throwaway location with `-DistExe`, `-InstallDir`,
  `-BuildLog`, `-PackageDir` and `-NoShortcuts` (all default to the real paths).
- Creates `%LOCALAPPDATA%\Programs\OpenOBD` if needed.
- If OpenOBD is running from that folder, it asks each window to close.
  Unsaved calibration edits bring up the "Save before closing?" prompt. It
  waits up to 60 seconds. If anything is still open, it stops with
  "deploy ABORTED" and copies nothing. It never force-kills the app and
  never installs beside a running copy. Answer the prompt (or close the app)
  and run it again.
- Backs up the installed exe (step 6), copies `dist\openobd.exe` over
  `OpenOBD.exe`, and fails with "deploy VERIFY FAILED" if the installed
  version resource differs from the built one.
- Creates or repoints the `OpenOBD` shortcut in the Start menu and on the
  Desktop at the installed exe, so either launches exactly what was just
  deployed.

## 8. Check the installed copy

```powershell
(Get-Item "$env:LOCALAPPDATA\Programs\OpenOBD\OpenOBD.exe").VersionInfo.ProductVersion
```

This must match the version from step 5. Then start OpenOBD from the Start
menu or Desktop shortcut and confirm it opens.

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
