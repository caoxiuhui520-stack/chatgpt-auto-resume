# ChatGPT Auto Resume - run in the foreground
#
# Useful for observing a full poll cycle in a console window. Press Ctrl+C to
# stop. The scheduled task is unaffected.
#
# Usage:
#     powershell -ExecutionPolicy Bypass -File scripts\run.ps1
#     powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Provider fake -Ticks 4

param(
    [string]$Provider = '',
    [string]$ChatGpt = '',
    [int]$Ticks = 0
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "virtual environment not found; run scripts\install.ps1 first"
}

$arguments = @('-m', 'app.main')

if ($Ticks -gt 0) {
    $arguments += 'once'
    $arguments += @('--ticks', $Ticks)
} else {
    $arguments += 'run'
}
if ($Provider) { $arguments += @('--provider', $Provider) }
if ($ChatGpt)  { $arguments += @('--chatgpt', $ChatGpt) }

Write-Host "running: $python $($arguments -join ' ')" -ForegroundColor Cyan
& $python @arguments
