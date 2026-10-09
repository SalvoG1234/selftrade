<#
  selftrade - control the tracked loop (start / pause / resume / stop / status).
  Examples:
    .\control.ps1 status
    .\control.ps1 pause
    .\control.ps1 resume
    .\control.ps1 stop
#>
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("status", "start", "pause", "resume", "stop")]
  [string]$Action,
  [int]$Generations = 0
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "virtualenv not found. Run .\setup.ps1 first." }

Push-Location $root
try {
  if ($Generations -gt 0) {
    & $venvPy -m selftrade session $Action --generations $Generations
  } else {
    & $venvPy -m selftrade session $Action
  }
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
