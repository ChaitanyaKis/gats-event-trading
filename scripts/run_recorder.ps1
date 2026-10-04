# Keeps the GATS recorder running on Windows: restarts it if it exits.
# Usage (from the repo root, venv already created):
#   powershell -ExecutionPolicy Bypass -File scripts\run_recorder.ps1
# Stop with Ctrl+C: that ends the recorder AND this loop (verified 2026-10-04).
# The loop is for crashes: if the recorder exits by itself, it is started again.
# To restart after a code update: Ctrl+C, then run this script again.

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$gats = Join-Path $repo ".venv\Scripts\gats.exe"

if (-not (Test-Path $gats)) {
    Write-Error "gats not found at $gats. Create the venv and run: pip install -e .[dev]"
    exit 1
}

while ($true) {
    Write-Host "$(Get-Date -Format s) starting recorder"
    & $gats record
    Write-Host "$(Get-Date -Format s) recorder exited with code $LASTEXITCODE; restarting in 30 s"
    Start-Sleep -Seconds 30
}
