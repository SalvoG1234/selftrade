<#
  selftrade - local dashboard (loopback only).
  Usage:
    .\dashboard.ps1
    .\dashboard.ps1 -Port 8788
#>
param(
  [string]$BindHost = "127.0.0.1",
  [int]$Port = 0
)
# NOTE: $BindHost, not $Host - $Host is a reserved PowerShell automatic variable.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "virtualenv not found. Run .\setup.ps1 first." }

$a = @("serve", "--host", $BindHost)
if ($Port -gt 0) { $a += @("--port", "$Port") }

Push-Location $root
try {
  & $venvPy -m selftrade @a
  exit $LASTEXITCODE
} finally {
  Pop-Location
}
