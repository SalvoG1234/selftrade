<#
  selftrade - MetaTrader 5 / Trading.com bridge.
  Examples:
    .\mt5.ps1 doctor
    .\mt5.ps1 symbols --symbol EURUSD
    .\mt5.ps1 bars --out data/EURUSD_1h.csv
    .\mt5.ps1 costs --symbol EURUSD
    .\mt5.ps1 order --symbol EURUSD --side buy --sl-points 500       # dry run
    .\mt5.ps1 order --symbol EURUSD --side buy --sl-points 500 --live # needs mt5.live too
#>
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "virtualenv not found. Run .\setup.ps1 first." }
if (-not $Args -or $Args.Count -eq 0) { $Args = @("doctor") }

Push-Location $root
try {
  & $venvPy -m selftrade mt5 @Args
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
