<#
  selftrade - Windows 11 setup.
  Creates .venv, installs core + dev dependencies, and optionally the MT5 bridge.

  Usage:
    powershell -ExecutionPolicy Bypass -File .\setup.ps1
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithMt5
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -NoDev
#>
param(
  [switch]$WithMt5,
  [switch]$NoDev
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Get-Python {
  if (Get-Command py -ErrorAction SilentlyContinue) { return @("py", "-3") }
  if (Get-Command python -ErrorAction SilentlyContinue) { return @("python") }
  throw "Python 3.11+ not found on PATH. Install it from python.org and retry."
}

$py = Get-Python
$pyExe = $py[0]
$pyArgs = @()
if ($py.Length -gt 1) { $pyArgs = $py[1..($py.Length-1)] }

Write-Host "selftrade setup" -ForegroundColor Cyan
& $pyExe @pyArgs --version

if (-not (Test-Path ".venv")) {
  Write-Host "creating .venv..." -ForegroundColor Yellow
  & $pyExe @pyArgs -m venv .venv
} else {
  Write-Host ".venv already exists" -ForegroundColor DarkGray
}

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip

Write-Host "installing core requirements..." -ForegroundColor Yellow
& $venvPy -m pip install -r requirements.txt

if (-not $NoDev) {
  Write-Host "installing dev requirements..." -ForegroundColor Yellow
  & $venvPy -m pip install -r requirements-dev.txt
}

if ($WithMt5) {
  Write-Host "installing MetaTrader5 (Windows-only)..." -ForegroundColor Yellow
  & $venvPy -m pip install -r requirements-mt5.txt
}

New-Item -ItemType Directory -Force -Path data, state, reports, user_data\strategies | Out-Null

Write-Host ""
Write-Host "Done. Next steps:" -ForegroundColor Green
Write-Host "  .\run.ps1 doctor"
Write-Host "  .\run.ps1 data --rows 12000"
Write-Host "  .\run.ps1 evolve --generations 5 --no-llm"
