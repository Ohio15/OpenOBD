# Deploy dist\openobd.exe to the install dir.
#
# Policy (Ron, 2026-08-21): NEVER rename-swap around a running instance. If the
# app is running, ask it to close gracefully - Qt's closeEvent shows the
# "Save before closing?" prompt for unsaved calibration edits - and wait. If an
# instance stays open (the user cancelled the prompt), ABORT the deploy; never
# force-kill and never deploy beside a running copy.
$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "dist\openobd.exe"
$destDir = Join-Path $env:LOCALAPPDATA "Programs\OpenOBD"
$dest = Join-Path $destDir "OpenOBD.exe"

if (-not (Test-Path $src)) { throw "no build at $src - run _build.bat first (see docs\RELEASE.md)" }
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
