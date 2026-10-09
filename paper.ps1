<#
  selftrade - forward paper trading.
  Examples:
    .\paper.ps1 -Create
    .\paper.ps1 -Run
    .\paper.ps1 -All
    .\paper.ps1 -Status
    .\paper.ps1 -Reset
#>
param(
  [switch]$Create,
  [switch]$Run,
  [switch]$All,
  [switch]$Status,
  [switch]$Reset,
  [int]$Session = 0,
  [switch]$Json
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "virtualenv not found. Run .\setup.ps1 first." }

$a = @()
if ($Create) { $a += "--create" }
if ($Run)    { $a += "--run" }
if ($All)    { $a += "--all" }
if ($Status) { $a += "--status" }
if ($Reset)  { $a += "--reset" }
if ($Json)   { $a += "--json" }
if ($Session -gt 0) { $a += @("--session", "$Session") }
if ($a.Count -eq 0) { $a += "--status" }

Push-Location $root
try {
  & $venvPy -m selftrade paper @a
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
