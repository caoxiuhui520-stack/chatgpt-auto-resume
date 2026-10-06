# ChatGPT Auto Resume - installer
#
# Creates the virtual environment, installs dependencies, seeds config.yaml,
# creates the log directory and registers the scheduled task.
#
# Usage (from the project root):
#     powershell -ExecutionPolicy Bypass -File scripts\install.ps1

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "ChatGPT Auto Resume - install" -ForegroundColor Cyan
Write-Host "project root: $ProjectRoot"

# --- 1. Python -------------------------------------------------------------
$python = $null
$venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (Test-Path $venvPython) {
    $python = $venvPython
    Write-Host "[1/5] reusing existing virtual environment"
} else {
    $candidates = @('py -3.12', 'py -3.13', 'python')
    foreach ($candidate in $candidates) {
        try {
            $parts = $candidate.Split(' ')
            if ($parts.Length -gt 1) {
                & $parts[0] $parts[1..($parts.Length - 1)] --version *> $null
            } else {
                & $candidate --version *> $null
            }
            $base = $candidate
            break
        } catch { continue }
    }
    if (-not $base) { throw 'Python 3.12+ was not found on PATH.' }

    Write-Host "[1/5] creating virtual environment with $base"
    if ($base -like 'py *') {
        $parts = $base.Split(' ')
        & $parts[0] $parts[1..($parts.Length - 1)] -m venv .venv
    } else {
        & $base -m venv .venv
    }
    $python = $venvPython
}

if (-not (Test-Path $python)) { throw "virtual environment python not found: $python" }

# --- 2. dependencies ------------------------------------------------------
Write-Host "[2/5] installing dependencies"
& $python -m pip install --disable-pip-version-check --upgrade pip | Out-Null
& $python -m pip install --disable-pip-version-check -r requirements.txt

# --- 3. configuration -----------------------------------------------------
if (-not (Test-Path (Join-Path $ProjectRoot 'config.yaml'))) {
    Copy-Item (Join-Path $ProjectRoot 'config.example.yaml') (Join-Path $ProjectRoot 'config.yaml')
    Write-Host "[3/5] config.yaml created (dry_run is ON by default)"
} else {
    Write-Host "[3/5] config.yaml already exists, left untouched"
}

# --- 4. directories -------------------------------------------------------
foreach ($dir in @('logs', 'data')) {
    $path = Join-Path $ProjectRoot $dir
    if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
}
Write-Host "[4/5] logs/ and data/ ready"

# --- 5. scheduled task ----------------------------------------------------
Write-Host "[5/5] registering the scheduled task"
& $python -m app.main install-autostart

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Watch it in dry-run mode first:"
Write-Host "       .\.venv\Scripts\python.exe -m app.main once --ticks 3"
Write-Host "  2. Run it in the foreground to observe a full cycle:"
Write-Host "       powershell -ExecutionPolicy Bypass -File scripts\run.ps1"
Write-Host "  3. Only after the logs look right, set 'dry_run: false' in config.yaml."
Write-Host ""
Write-Host "Safety: the program never extends or bypasses a quota limit."
