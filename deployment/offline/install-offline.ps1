[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EnvironmentDirectory,

    [Parameter(Mandatory = $false)]
    [string]$PythonExecutable = 'python'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$bundleRoot = $PSScriptRoot
$wheelhouse = Join-Path $bundleRoot 'wheelhouse'
if (-not (Test-Path -LiteralPath $wheelhouse -PathType Container)) {
    throw 'Bundle wheelhouse is missing.'
}
if (Test-Path -LiteralPath $EnvironmentDirectory) {
    throw 'EnvironmentDirectory already exists; choose a new path.'
}

& $PythonExecutable -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 'Python 3.12+ is required')"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12+ check failed.' }
& $PythonExecutable -m venv $EnvironmentDirectory
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }

$venvPython = Join-Path $EnvironmentDirectory 'Scripts\python.exe'
& $venvPython -m pip install --no-index --find-links $wheelhouse nexus-jar-sync
if ($LASTEXITCODE -ne 0) { throw 'Offline installation failed; verify compatible wheels are present.' }
Write-Host "Installed nexus-jar-sync into $EnvironmentDirectory"
