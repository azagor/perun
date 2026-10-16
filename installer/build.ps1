#requires -Version 5.1
<#
.SYNOPSIS
    Build the Perun Windows installer.

.DESCRIPTION
    Compiles installer\PerunSetup.iss with the Inno Setup compiler and produces
    installer\Output\PerunSetup.exe.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$iscc = Get-ChildItem -Path 'C:\Program Files (x86)', 'C:\Program Files', "$env:LOCALAPPDATA\Programs" -Filter 'ISCC.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $iscc) {
    Write-Host "Inno Setup compiler (ISCC.exe) not found. Install Inno Setup first." -ForegroundColor Red
    exit 1
}

$iss = Join-Path $PSScriptRoot 'PerunSetup.iss'
if (-not (Test-Path -LiteralPath $iss)) {
    Write-Host "PerunSetup.iss not found at $iss" -ForegroundColor Red
    exit 1
}

Write-Host "Building Perun installer with $($iscc.FullName) ..."
& $iscc.FullName $iss
$rc = $LASTEXITCODE
if ($rc -ne 0) {
    Write-Host "Build failed (exit code $rc)." -ForegroundColor Red
    exit $rc
}

$exe = Join-Path $PSScriptRoot 'Output\PerunSetup.exe'
if (Test-Path -LiteralPath $exe) {
    Write-Host "Installer built: $exe" -ForegroundColor Green
} else {
    Write-Host "Build completed but $exe was not found." -ForegroundColor Yellow
}
