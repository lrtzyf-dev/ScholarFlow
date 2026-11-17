param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Wheelhouse,
    [Parameter(Mandatory=$true)][string]$Destination
)
$ErrorActionPreference = 'Stop'
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) { throw 'Destination must be a new directory; no environment will be overwritten.' }
$wheelhousePath = (Resolve-Path -LiteralPath $Wheelhouse).Path
$lockPath = Join-Path $PSScriptRoot 'requirements-win-amd64-cp314.lock'
& $Python -c "import sys,platform; assert sys.version_info[:3]==(3,14,3) and sys.platform=='win32' and platform.machine().upper()=='AMD64', 'Expected CPython 3.14.3 / Windows AMD64'"
if ($LASTEXITCODE -ne 0) { throw 'Runtime does not match the locked release environment.' }
& $Python (Join-Path $PSScriptRoot 'release_tool.py') verify-wheel-lock $wheelhousePath --lock $lockPath
if ($LASTEXITCODE -ne 0) { throw 'Wheelhouse integrity verification failed.' }
& $Python -m venv $destinationPath
if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated environment; the incomplete directory was retained for inspection.' }
$releasePython = Join-Path $destinationPath 'Scripts/python.exe'
$previousPipConfig = $env:PIP_CONFIG_FILE
try {
    $env:PIP_CONFIG_FILE = 'NUL'
    & $releasePython -m pip --isolated install --no-index --only-binary=:all: --force-reinstall --find-links $wheelhousePath --require-hashes -r $lockPath --report (Join-Path $destinationPath 'offline-install.json')
    if ($LASTEXITCODE -ne 0) { throw 'Offline hash-locked installation failed; no existing installation was changed.' }
    & $releasePython -m pip --isolated check
    if ($LASTEXITCODE -ne 0) { throw 'Installed dependency constraints are not satisfied.' }
} finally {
    $env:PIP_CONFIG_FILE = $previousPipConfig
}
Write-Output "Offline release environment ready: $destinationPath"
