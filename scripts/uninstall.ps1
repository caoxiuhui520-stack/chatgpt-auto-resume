# ChatGPT Auto Resume - uninstaller
#
# Removes the scheduled task. By default it does NOT remove config.yaml, the
# data directory or the logs - those are the user's, and deleting them would
# silently discard the duplicate-protection state.
#
# Usage:
#     powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
#     powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -Purge

param(
    [switch]$Purge
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "ChatGPT Auto Resume - uninstall" -ForegroundColor Cyan

$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (Test-Path $python) {
    & $python -m app.main uninstall-autostart
} else {
    $taskName = 'ChatGPTAutoResume'
    Write-Host "venv not found; removing the scheduled task directly: $taskName"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}

if ($Purge) {
    Write-Host "PURGE requested: removing data/ and logs/" -ForegroundColor Yellow
    foreach ($dir in @('data', 'logs')) {
        $path = Join-Path $ProjectRoot $dir
        if (Test-Path $path) {
            Remove-Item -Recurse -Force $path
            Write-Host "  removed $dir"
        }
    }
    Write-Host "config.yaml was preserved even with -Purge; delete it manually if you want."
} else {
    Write-Host "config.yaml, data/ and logs/ were preserved."
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
