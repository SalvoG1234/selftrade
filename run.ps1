<#
  selftrade - run any CLI command in the project virtualenv.
  Examples:
    .\run.ps1 doctor
    .\run.ps1 evolve --generations 5 --no-llm
    .\run.ps1 backtest --champion --holdout
#>
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  throw "virtualenv not found. Run .\setup.ps1 first."
}
Push-Location $root
try {
  & $venvPy -m selftrade @Args
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
