#requires -Version 5.1
<#
.SYNOPSIS
    Sign the Perun installer.

.DESCRIPTION
    Signs installer\Output\PerunSetup.exe with signtool.

    By default it creates and uses a self-signed certificate. For release
    builds, set PERUN_SIGN_PFX (path to a PFX) or PERUN_SIGN_PFX_BASE64
    (base64-encoded PFX content) and PERUN_SIGN_PASSWORD via environment
    variables. This works with certificates from Certum, SSL.com, Sectigo,
    DigiCert, Microsoft Trusted Signing exports, etc.

.PARAMETER PfxPath
    Path to a code-signing PFX. If omitted, the script looks for the
    PERUN_SIGN_PFX or PERUN_SIGN_PFX_BASE64 environment variables, then
    falls back to creating a self-signed certificate.

.PARAMETER Password
    Password for the PFX. Falls back to the PERUN_SIGN_PASSWORD environment
    variable. Required when using an external PFX.

.PARAMETER Subject
    Subject for the self-signed fallback certificate.
    Defaults to "CN=Perun in The Machine".

.PARAMETER NoSelfSign
    Do not create a self-signed certificate if no external PFX is provided.
#>
[CmdletBinding()]
param(
    [string]$PfxPath,
    [string]$Password,
    [string]$Subject = 'CN=Perun in The Machine',
    [switch]$NoSelfSign
)

$ErrorActionPreference = 'Stop'

$exe = Join-Path $PSScriptRoot 'Output\PerunSetup.exe'
if (-not (Test-Path -LiteralPath $exe)) {
    Write-Host "Installer not found: $exe" -ForegroundColor Red
    Write-Host "Build it first with: .\installer\build.ps1"
    exit 1
}

# --------------------------------------------------------------------------- #
# Locate signtool.exe (x64)
# --------------------------------------------------------------------------- #
$signTool = Get-ChildItem -Path 'C:\Program Files (x86)\Windows Kits','C:\Program Files\Windows Kits' `
    -Filter 'signtool.exe' -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -like '*\x64\*' } |
    Select-Object -First 1
if (-not $signTool) {
    Write-Host "signtool.exe (x64) not found. Install the Windows SDK." -ForegroundColor Red
    exit 1
}

# --------------------------------------------------------------------------- #
# Resolve PFX source: parameter -> env var -> base64 env var -> self-signed
# --------------------------------------------------------------------------- #
$tempPfx = $null
$usingSelfSigned = $false

if ($PfxPath -and (Test-Path -LiteralPath $PfxPath)) {
    Write-Host "Using supplied PFX: $PfxPath"
} elseif ($env:PERUN_SIGN_PFX -and (Test-Path -LiteralPath $env:PERUN_SIGN_PFX)) {
    $PfxPath = $env:PERUN_SIGN_PFX
    Write-Host "Using PFX from PERUN_SIGN_PFX: $PfxPath"
} elseif ($env:PERUN_SIGN_PFX_BASE64) {
    $tempPfx = Join-Path $env:TEMP ('perun-sign-' + [guid]::NewGuid().ToString('n') + '.pfx')
    $bytes = [Convert]::FromBase64String($env:PERUN_SIGN_PFX_BASE64)
    [IO.File]::WriteAllBytes($tempPfx, $bytes)
    $PfxPath = $tempPfx
    Write-Host "Using PFX from PERUN_SIGN_PFX_BASE64 (temp: $tempPfx)"
} else {
    if ($NoSelfSign) {
        Write-Host "No external PFX provided and -NoSelfSign specified. Aborting." -ForegroundColor Red
        exit 1
    }
    $usingSelfSigned = $true
    $PfxPath = Join-Path $PSScriptRoot 'certs\PerunSelfSign.pfx'
    Write-Host "No external PFX provided; falling back to self-signed certificate."
}

if (-not (Test-Path -LiteralPath $PfxPath)) {
    Write-Host "PFX not found: $PfxPath" -ForegroundColor Red
    exit 1
}

# --------------------------------------------------------------------------- #
# Resolve password
# --------------------------------------------------------------------------- #
if (-not $Password -and $env:PERUN_SIGN_PASSWORD) {
    $Password = $env:PERUN_SIGN_PASSWORD
}

if ($usingSelfSigned) {
    # Self-signed path: create the cert and export with a random or supplied password.
    if (-not $Password) {
        $Password = -join ((1..16) | ForEach-Object { Get-Random -Maximum 16 | ForEach-Object { "0123456789abcdef"[$_] } })
        Write-Host "Self-signed PFX password (save if needed): $Password" -ForegroundColor Yellow
    }

    $cert = Get-ChildItem -Path Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Subject -eq $Subject } |
        Select-Object -First 1

    if (-not $cert) {
        Write-Host "Creating self-signed code-signing certificate: $Subject"
        $cert = New-SelfSignedCertificate `
            -Type CodeSigningCert `
            -Subject $Subject `
            -KeyUsage DigitalSignature `
            -KeyAlgorithm RSA `
            -KeyLength 2048 `
            -CertStoreLocation Cert:\CurrentUser\My `
            -NotAfter (Get-Date).AddYears(5)
        Write-Host "Created certificate thumbprint: $($cert.Thumbprint)"
    } else {
        Write-Host "Reusing existing self-signed certificate: $($cert.Thumbprint)"
    }

    $pfxDir = Split-Path -Parent $PfxPath
    if (-not (Test-Path -LiteralPath $pfxDir)) {
        New-Item -ItemType Directory -Force -Path $pfxDir | Out-Null
    }
    $securePwd = ConvertTo-SecureString -String $Password -Force -AsPlainText
    Export-PfxCertificate -Cert $cert -FilePath $PfxPath -Password $securePwd -Force | Out-Null
    Write-Host "Exported self-signed certificate to $PfxPath"
} elseif (-not $Password) {
    Write-Host "Password is required when using an external PFX. Set -Password or PERUN_SIGN_PASSWORD." -ForegroundColor Red
    exit 1
}

# --------------------------------------------------------------------------- #
# Sign the installer
# --------------------------------------------------------------------------- #
Write-Host "Signing $exe ..."
& $signTool.FullName sign `
    /f "$PfxPath" `
    /p "$Password" `
    /fd sha256 `
    /tr http://timestamp.digicert.com `
    /td sha256 `
    "$exe"

$rc = $LASTEXITCODE

if ($tempPfx -and (Test-Path -LiteralPath $tempPfx)) {
    Remove-Item -LiteralPath $tempPfx -Force
}

if ($rc -ne 0) {
    Write-Host "Signing failed." -ForegroundColor Red
    exit 1
}

Write-Host "Signed successfully." -ForegroundColor Green
if ($usingSelfSigned) {
    Write-Host "Note: self-signed signature is not trusted by default on other machines."
}
