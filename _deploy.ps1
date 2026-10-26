# Deploy dist\openobd.exe to the install dir.
#
# Policy (Ron, 2026-08-21): NEVER rename-swap around a running instance. If the
# app is running, ask it to close gracefully - Qt's closeEvent shows the
# "Save before closing?" prompt for unsaved calibration edits - and wait. If an
# instance stays open (the user cancelled the prompt), ABORT the deploy; never
# force-kill and never deploy beside a running copy.
#
# Pre-deploy gate (v0.34.1, discovery 455efc83): a stale dist\openobd.exe once
# overwrote a newer install because the build step had silently not run. Before
# anything is touched (no directory, no backup, no copy) the script refuses
# unless ALL of these hold:
#   1. build.log ends a successful build: its last BUILD_DONE_EXIT line is _0
#      and its last VERIFY_DONE_EXIT line is present, after it, and is _0.
#   2. dist exe ProductVersion == __version__ in openobd\__init__.py.
#   3. dist exe is not older than the newest source file under the package
#      folder (ignoring __pycache__), i.e. nothing changed since it was built.
#   4. dist exe was written by the build that produced build.log: its mtime is
#      within 10 minutes of build.log's last write. (The exe is written BEFORE
#      the BUILD_DONE/VERIFY_DONE lines, so "exe newer than the log" can never
#      hold; the mtime pairing is what ties the two files to one build.)
# Parameters default to the real locations; tests pass throwaway ones.
param(
    [string]$DistExe    = (Join-Path $PSScriptRoot "dist\openobd.exe"),
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\OpenOBD"),
    [string]$BuildLog   = (Join-Path $PSScriptRoot "build.log"),
    [string]$PackageDir = (Join-Path $PSScriptRoot "openobd"),
    [switch]$NoShortcuts
)
$ErrorActionPreference = "Stop"
$src = $DistExe
$destDir = $InstallDir
$dest = Join-Path $destDir "OpenOBD.exe"

function Refuse([string]$why) { throw "deploy REFUSED (nothing copied, no backup taken): $why" }

if (-not (Test-Path $src)) { Refuse "no build at $src - run _build.bat first (see docs\RELEASE.md)" }

if (-not (Test-Path $BuildLog)) { Refuse "no build log at $BuildLog - run _build.bat first" }
$logText = Get-Content -Raw -Path $BuildLog
$buildDone  = @([regex]::Matches($logText, '(?m)^BUILD_DONE_EXIT_(\d+)\s*$'))
$verifyDone = @([regex]::Matches($logText, '(?m)^VERIFY_DONE_EXIT_(\d+)\s*$'))
if (-not $buildDone.Count) { Refuse "build.log has no BUILD_DONE_EXIT line - the build did not finish; rerun _build.bat" }
$lastBuild = $buildDone[$buildDone.Count - 1]
if ($lastBuild.Groups[1].Value -ne "0") { Refuse "build.log last BUILD_DONE_EXIT_$($lastBuild.Groups[1].Value) is non-zero - the build failed" }
if (-not $verifyDone.Count) { Refuse "build.log has no VERIFY_DONE_EXIT line - the build was not version-verified; rerun _build.bat" }
$lastVerify = $verifyDone[$verifyDone.Count - 1]
if ($lastVerify.Index -lt $lastBuild.Index) { Refuse "build.log VERIFY_DONE_EXIT precedes the last BUILD_DONE_EXIT - rerun _build.bat" }
if ($lastVerify.Groups[1].Value -ne "0") { Refuse "build.log last VERIFY_DONE_EXIT_$($lastVerify.Groups[1].Value) is non-zero - the exe failed version verification" }

$initFile = Join-Path $PackageDir "__init__.py"
if (-not (Test-Path $initFile)) { Refuse "cannot find $initFile to read __version__" }
$m = [regex]::Match((Get-Content -Raw -Path $initFile), '__version__\s*=\s*"(\d+\.\d+\.\d+)"')
if (-not $m.Success) { Refuse "cannot read __version__ from $initFile" }
$pkgVer = $m.Groups[1].Value
$exeVer = (Get-Item $src).VersionInfo.ProductVersion
if ($exeVer -ne $pkgVer) { Refuse "$src is v$exeVer but the package is v$pkgVer - it is a stale or wrong build; run _build.bat" }

$exeTime = (Get-Item $src).LastWriteTimeUtc
$newest = Get-ChildItem -Path $PackageDir -Recurse -File -ErrorAction Stop |
          Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -ne '.pyc' } |
          Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if ($newest -and $newest.LastWriteTimeUtc -gt $exeTime) {
    Refuse "$($newest.FullName) was modified after $src was built - rebuild with _build.bat"
}
$gapSeconds = [math]::Abs(((Get-Item $BuildLog).LastWriteTimeUtc - $exeTime).TotalSeconds)
if ($gapSeconds -gt 600) {
    Refuse "$src was not written by the build recorded in $BuildLog (mtimes differ by $([int]$gapSeconds) s) - rerun _build.bat"
}
Write-Host "pre-deploy gate passed: exe v$exeVer == package v$pkgVer, build.log clean, exe newer than sources"

if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Force $destDir | Out-Null }

$running = @(Get-Process -ErrorAction SilentlyContinue |
             Where-Object { $_.Path -like "$destDir\*" })
if ($running.Count) {
    Write-Host "OpenOBD is running (PID $($running.Id -join ', ')) - requesting close (save prompt may appear)..."
    foreach ($p in $running) { $null = $p.CloseMainWindow() }
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $running = @(Get-Process -ErrorAction SilentlyContinue |
                     Where-Object { $_.Path -like "$destDir\*" })
        if (-not $running.Count) { break }
    }
    if ($running.Count) {
        throw "deploy ABORTED: instance(s) still open (PID $($running.Id -join ', ')) - close them (or answer the save prompt) and rerun"
    }
}

# Keep the outgoing exe so docs\RELEASE.md step 9 always has a rollback copy.
if (Test-Path $dest) {
    $old = Get-Item $dest
    $oldVer = $old.VersionInfo.ProductVersion
    if (-not $oldVer) { $oldVer = "unknown-" + $old.LastWriteTime.ToString("yyyyMMdd-HHmmss") }
    $backupDir = Join-Path $destDir "backup"
    New-Item -ItemType Directory -Force $backupDir | Out-Null
    Copy-Item $dest (Join-Path $backupDir "OpenOBD-$oldVer.exe") -Force
    Write-Host "backed up $oldVer to $backupDir"
}

Copy-Item $src $dest -Force
$ver = (Get-Item $dest).VersionInfo.ProductVersion
$want = (Get-Item $src).VersionInfo.ProductVersion
if ($ver -ne $want) { throw "deploy VERIFY FAILED: installed $ver, built $want" }
Write-Host "deployed $dest (v$ver)"

if ($NoShortcuts) { Write-Host "shortcuts skipped (-NoShortcuts)"; return }

# Start menu and Desktop shortcuts always point at the installed exe, so
# launching OpenOBD runs exactly what was just deployed. GetFolderPath follows
# a OneDrive-redirected Desktop.
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in "Programs", "Desktop") {
    $lnk = Join-Path ([Environment]::GetFolderPath($folder)) "OpenOBD.lnk"
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $dest
    $s.WorkingDirectory = $destDir
    $s.IconLocation = "$dest,0"
    $s.Description = "OpenOBD v$ver"
    $s.Save()
    Write-Host "shortcut $lnk -> $dest"
}
