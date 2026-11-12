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

# Build the VS Code companion so a locally built installer matches the CI one.
# The VSIX is gitignored, so a fresh clone has to produce it here.
$vsix = Join-Path $PSScriptRoot 'vscode\perun-overseer.vsix'
if (-not (Test-Path -LiteralPath $vsix)) {
    $ext = Join-Path (Split-Path -Parent $PSScriptRoot) 'extensions\vscode'
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue) -and
        -not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "WARNING: npm was not found, so the VS Code companion cannot be built." -ForegroundColor Yellow
        Write-Host "         The installer will ship without it. Download perun-overseer.vsix" -ForegroundColor Yellow
        Write-Host "         from the GitHub release, or install Node and rebuild." -ForegroundColor Yellow
    } else {
        Write-Host "Building the VS Code companion ..."
        Push-Location $ext
        try {
            & npm ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
            & npm run package
            if ($LASTEXITCODE -ne 0) { throw "npm run package failed with exit code $LASTEXITCODE" }
        } finally {
            Pop-Location
        }
        if (-not (Test-Path -LiteralPath $vsix)) {
            throw "VSIX was not produced at $vsix"
        }
    }
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
