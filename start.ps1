<#
.SYNOPSIS
  One-shot launcher for KalshiTrader on Windows: sets up, then runs the bot and dashboard.

.DESCRIPTION
  Run from PowerShell:   .\start.ps1
  Or double-click start.bat.

  What it does (each step is skipped if already done):
    1. checks Python 3.10+
    2. creates .venv and installs KalshiTrader into it
    3. creates .env from .env.example (demo API, paper trading, $1,000 fake cash)
    4. opens the dashboard in a second window and in your browser
    5. runs the trading loop in this window (Ctrl-C to stop)

.PARAMETER SetupOnly
  Install and configure, but do not launch anything.
.PARAMETER NoBrowser
  Do not open the dashboard in the browser.
.PARAMETER Once
  Run a single trading cycle instead of the loop.
.PARAMETER Scan
  Run a read-only scan (prints signals, places nothing) instead of the loop.
.PARAMETER Port
  Dashboard port (default 8000).
#>
[CmdletBinding()]
param(
    [switch]$SetupOnly,
    [switch]$NoBrowser,
    [switch]$Once,
    [switch]$Scan,
    [int]$Port = 8000
)

# Native commands write progress to stderr; keep PowerShell from treating that as fatal and check exit codes explicitly.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# ---- 1. Python -------------------------------------------------------------
$python = $null
foreach ($candidate in @("py -3", "python", "python3")) {
    try {
        $ver = & cmd /c "$candidate -c ""import sys; print(sys.version_info[0]*100+sys.version_info[1])""" 2>$null
        if ($LASTEXITCODE -eq 0 -and [int]$ver -ge 310) { $python = $candidate; break }
    } catch {}
}
if (-not $python) {
    Write-Host "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this again." -ForegroundColor Red
    exit 1
}
Write-Step "Using $python"

# ---- 2. Virtual environment + install -------------------------------------
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Step "Creating virtual environment in .venv"
    & cmd /c "$python -m venv .venv"
    if ($LASTEXITCODE -ne 0) { Write-Host "venv creation failed" -ForegroundColor Red; exit 1 }
}
# Reinstall when the package will not import or pyproject.toml changed since the last install.
$stamp = Join-Path $PSScriptRoot ".venv\.kalshitrader-install-stamp"
& $venvPython -c "import kalshitrader.cli" 2>$null
$needInstall = ($LASTEXITCODE -ne 0) -or (-not (Test-Path $stamp)) -or ((Get-Item "pyproject.toml").LastWriteTimeUtc -gt (Get-Item $stamp).LastWriteTimeUtc)
if ($needInstall) {
    Write-Step "Installing KalshiTrader and its dependencies (takes a minute)"
    & $venvPython -m pip install --quiet --upgrade pip
    & $venvPython -m pip install --quiet -e ".[dev,ai]"
    if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed" -ForegroundColor Red; exit 1 }
    Set-Content -Path $stamp -Value (Get-Date -Format o)
}

# ---- 3. Config -------------------------------------------------------------
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Step "Created .env (demo API, paper trading). Edit it to change risk limits or add API keys."
}
New-Item -ItemType Directory -Force -Path "data" | Out-Null

if ($SetupOnly) { Write-Step "Setup complete. Run .\start.ps1 to launch."; exit 0 }

# ---- 4. Scan / once shortcuts ----------------------------------------------
if ($Scan) { & $venvPython -m kalshitrader scan --all; exit $LASTEXITCODE }
if ($Once) { & $venvPython -m kalshitrader run --once; exit $LASTEXITCODE }

# ---- 5. Dashboard in a second window, bot in this one ----------------------
Write-Step "Starting dashboard on http://127.0.0.1:$Port"
$dashArgs = @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command",
    "Set-Location '$PSScriptRoot'; & '$venvPython' -m kalshitrader dashboard --port $Port")
Start-Process -FilePath "powershell.exe" -ArgumentList $dashArgs | Out-Null
Start-Sleep -Seconds 2
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }

Write-Step "Starting the trading loop (paper mode unless .env says otherwise). Ctrl-C to stop."
Write-Host "Tip: the bot starts OFF. Add your keys in the dashboard (Settings -> API keys), then press Resume." -ForegroundColor DarkGray
& $venvPython -m kalshitrader run
