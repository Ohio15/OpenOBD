# Deploy dist\openobd.exe to the install dir.
#
# Policy (Ron, 2026-08-21): NEVER rename-swap around a running instance. If the
# app is running, ask it to close gracefully — Qt's closeEvent shows the
# "Save before closing?" prompt for unsaved calibration edits — and wait. If an
# instance stays open (the user cancelled the prompt), ABORT the deploy; never
# force-kill and never deploy beside a running copy.
$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "dist\openobd.exe"
$destDir = Join-Path $env:LOCALAPPDATA "Programs\OpenOBD"
$dest = Join-Path $destDir "OpenOBD.exe"

if (-not (Test-Path $src)) { throw "no build at $src — run _build.bat first (see docs\RELEASE.md)" }
if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Force $destDir | Out-Null }

$running = @(Get-Process -ErrorAction SilentlyContinue |
             Where-Object { $_.Path -like "$destDir\*" })
if ($running.Count) {
    Write-Host "OpenOBD is running (PID $($running.Id -join ', ')) — requesting close (save prompt may appear)…"
    foreach ($p in $running) { $null = $p.CloseMainWindow() }
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $running = @(Get-Process -ErrorAction SilentlyContinue |
                     Where-Object { $_.Path -like "$destDir\*" })
        if (-not $running.Count) { break }
    }
    if ($running.Count) {
        throw "deploy ABORTED: instance(s) still open (PID $($running.Id -join ', ')) — close them (or answer the save prompt) and rerun"
    }
}

Copy-Item $src $dest -Force
$ver = (Get-Item $dest).LastWriteTime
Write-Host "deployed $dest ($ver)"
