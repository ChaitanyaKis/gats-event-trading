# Keeps the GATS paper runtime running on Windows: restarts it if it exits.
# Usage (from the repo root, with the recorder already running in its own
# terminal: it is the only source of filings):
#   powershell -ExecutionPolicy Bypass -File scripts\run_paper.ps1 -Name s1
# Stop with Ctrl+C twice (once for the runtime, once for this loop).
#
# Paper trading cannot send an order. If the runtime REFUSES to start (a
# changed design, a record the code no longer reproduces, no token), this
# script stops instead of looping: read the message, then use a new -Name.

param(
    [Parameter(Mandatory = $true)][string]$Name
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$gats = Join-Path $repo ".venv\Scripts\gats.exe"

if (-not (Test-Path $gats)) {
    Write-Error "gats not found at $gats. Create the venv and run: pip install -e .[dev]"
    exit 1
}

$quick = 0
while ($true) {
    $started = Get-Date
    Write-Host "$(Get-Date -Format s) starting paper run '$Name'"
    & $gats paper run --name $Name
    $ran = ((Get-Date) - $started).TotalSeconds
    Write-Host "$(Get-Date -Format s) paper run exited with code $LASTEXITCODE after $([int]$ran) s"
    if ($ran -lt 60) { $quick += 1 } else { $quick = 0 }
    if ($quick -ge 3) {
        Write-Error "it exited three times within a minute of starting: not restarting. Read the message above."
        exit 1
    }
    Start-Sleep -Seconds 15
}
