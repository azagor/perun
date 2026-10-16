#requires -Version 5.1
<#
.SYNOPSIS
    Reverse the Perun machine and global install during uninstall.
#>
param(
    [Parameter(Mandatory)]
    [string]$PerunDir
)

$ErrorActionPreference = 'Stop'
$StateFile = Join-Path $PerunDir '.perun-installer-state.json'

function Remove-EnvVar {
    param([string]$Name, [string]$Scope = 'Machine')
    $key = if ($Scope -eq 'Machine') { 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' } else { 'HKCU:\Environment' }
    if (Get-ItemProperty -Path $key -Name $Name -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $key -Name $Name -ErrorAction SilentlyContinue
    }
    [Environment]::SetEnvironmentVariable($Name, $null, $Scope)
}

if (-not (Test-Path -LiteralPath $StateFile)) { exit 0 }

$state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
$python = $state.python
if (-not $python -or -not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}

if ($python -and (Test-Path -LiteralPath $python)) {
    Write-Host "[perun] removing global OpenCode integration ..."
    & $python -m perun install --uninstall

    Write-Host "[perun] removing PerunBridge task ..."
    & $python -m perun install --machine --uninstall
}

Remove-EnvVar -Name 'PERUN_PYTHON' -Scope 'Machine'
Remove-EnvVar -Name 'PERUN_PYTHON' -Scope 'User'

Write-Host "[perun] uninstall cleanup complete."
