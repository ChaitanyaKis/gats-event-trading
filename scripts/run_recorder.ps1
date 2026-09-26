# Keeps the GATS recorder running on Windows: restarts it if it exits.
# Usage (from the repo root, venv already created):
#   powershell -ExecutionPolicy Bypass -File scripts\run_recorder.ps1
# Stop with Ctrl+C twice (once for the recorder, once for this loop).

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
