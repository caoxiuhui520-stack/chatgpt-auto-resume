# ChatGPT Auto Resume - launch the desktop control center (GUI)
#
# Usage:
#     powershell -ExecutionPolicy Bypass -File scripts\start-gui.ps1
#
# Requires the venv built by scripts\install.ps1 (which now also installs the
# GUI dependencies).

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "virtual environment not found; run scripts\install.ps1 first"
}

Write-Host "starting ChatGPT Auto Resume GUI..." -ForegroundColor Cyan
& $python -m app.gui
