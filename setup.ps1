#requires -Version 5.1
<#
.SYNOPSIS
    Quick-setup installer for Perun — clone (or reuse) the repo and run the
    machine installer.

.DESCRIPTION
    This script is designed to be run directly from the GitHub raw URL:

        powershell -c "iwr -useb https://raw.githubusercontent.com/azagor/perun/master/setup.ps1 | iex"

    It will:
      1. check for git and Python 3.10+
      2. clone the Perun repo into a target directory (or reuse an existing clone)
      3. delegate to bootstrap.ps1, which installs dependencies, wires the global
         OpenCode integration, registers the PerunBridge task, and creates a
         desktop shortcut

    If you already have a local copy of the repo, you can also run setup.ps1
    from inside it; in that case it skips the clone step and just runs the
    machine installer.

.PARAMETER InstallDir
    Directory where the repo should be cloned. Defaults to %USERPROFILE%\perun.

.PARAMETER RepoUrl
    Git URL to clone. Defaults to https://github.com/azagor/perun.git.

.PARAMETER Branch
    Branch to checkout after cloning. Defaults to master.

.PARAMETER Python
    Explicit path to python.exe. If omitted, PERUN_PYTHON or the first Python
    3.10+ on PATH is used.

.PARAMETER Force
    If InstallDir already exists and does not look like the Perun repo, remove
    it and re-clone.

.PARAMETER SkipDeps
    Do not run pip install -r requirements.txt.

.PARAMETER SkipGlobal
    Do not touch the global OpenCode config.

.PARAMETER SkipTask
    Do not register the PerunBridge scheduled task.

.PARAMETER NoIndex
    Do not (re)index project docs.

.PARAMETER NoShortcut
    Do not create the desktop shortcut.

.EXAMPLE
    # Web install (no repo needed)
    powershell -c "iwr -useb https://raw.githubusercontent.com/azagor/perun/master/setup.ps1 | iex"

.EXAMPLE
    # Local run from an existing checkout
    .\setup.ps1

.EXAMPLE
    # Clone to a custom location
    .\setup.ps1 -InstallDir C:\tools\perun
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:USERPROFILE 'perun'),
    [string]$RepoUrl = 'https://github.com/azagor/perun.git',
    [string]$Branch = 'master',
    [string]$Python,
    [switch]$Force,
    [switch]$SkipDeps,
    [switch]$SkipGlobal,
    [switch]$SkipTask,
    [switch]$NoIndex,
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Stop'

function Write-PerunStep {
    param([string]$Message)
    Write-Host "[perun] $Message" -ForegroundColor Cyan
}

# --------------------------------------------------------------------------- #
# Are we already inside a Perun checkout?
# --------------------------------------------------------------------------- #
$RepoRoot = $null
if ($PSScriptRoot) {
    $candidate = $PSScriptRoot
    if (Test-Path -LiteralPath (Join-Path $candidate 'perun\__main__.py')) {
        $RepoRoot = $candidate
        Write-PerunStep "running from existing checkout: $RepoRoot"
    }
}

# --------------------------------------------------------------------------- #
# Resolve Python before we clone, so we fail fast.
# --------------------------------------------------------------------------- #
function Resolve-PythonExe {
    param([string]$Explicit)

    $attempts = New-Object System.Collections.ArrayList
    if ($Explicit)         { [void]$attempts.Add(@{ cmd = $Explicit; pre = $null }) }
    if ($env:PERUN_PYTHON) { [void]$attempts.Add(@{ cmd = $env:PERUN_PYTHON; pre = $null }) }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py)     { [void]$attempts.Add(@{ cmd = $py.Source; pre = '-3' }) }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { [void]$attempts.Add(@{ cmd = $python.Source; pre = $null }) }

    foreach ($a in $attempts) {
        try {
            if ($a.pre) { $out = & $a.cmd $a.pre -c "import sys; print(sys.executable)" 2>$null }
            else        { $out = & $a.cmd -c "import sys; print(sys.executable)" 2>$null }
        } catch { $out = $null }
        $exe = ([string]($out | Select-Object -First 1)).Trim()
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { continue }

        $verText = ((& $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null) | Select-Object -First 1)
        if (-not $verText) { continue }
        $mm = ([string]$verText).Trim().Split('.')
        if ([int]$mm[0] -ge 3 -and [int]$mm[1] -ge 10) { return $exe }
        Write-Host "[perun] ignoring $exe (Python $verText is older than 3.10)" -ForegroundColor DarkYellow
    }
    return $null
}

$pyExe = Resolve-PythonExe -Explicit $Python
if (-not $pyExe) {
    Write-Host "[perun] No Python 3.10+ found." -ForegroundColor Red
    Write-Host "        Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
    Write-Host "        or set PERUN_PYTHON to your python.exe path, then re-run this script."
    exit 1
}
$env:PERUN_PYTHON = $pyExe

# --------------------------------------------------------------------------- #
# Clone (or reuse) the repo
# --------------------------------------------------------------------------- #
if (-not $RepoRoot) {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        Write-Host "[perun] git is required to clone the repo but was not found on PATH." -ForegroundColor Red
        Write-Host "        Install it with: winget install Git.Git"
        Write-Host "        Or clone the repo manually and run setup.ps1 from inside it."
        exit 1
    }

    if (Test-Path -LiteralPath $InstallDir) {
        $looksLikeRepo = Test-Path -LiteralPath (Join-Path $InstallDir 'perun\__main__.py')
        if ($looksLikeRepo) {
            Write-PerunStep "reusing existing clone at $InstallDir"
            $RepoRoot = $InstallDir
        } elseif ($Force) {
            Write-PerunStep "-Force set: removing $InstallDir"
            Remove-Item -Recurse -Force -LiteralPath $InstallDir
        } else {
            Write-Host "[perun] $InstallDir already exists and does not look like the Perun repo." -ForegroundColor Red
            Write-Host "        Pass -Force to replace it, or choose a different -InstallDir."
            exit 1
        }
    }

    if (-not $RepoRoot) {
        $parent = Split-Path -Parent $InstallDir
        if ($parent -and -not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
        }
        Write-PerunStep "cloning $RepoUrl into $InstallDir ..."
        & $git.Source clone --branch $Branch --single-branch $RepoUrl $InstallDir
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[perun] git clone failed." -ForegroundColor Red
            exit 1
        }
        $RepoRoot = $InstallDir
    }
}

Set-Location -LiteralPath $RepoRoot

# --------------------------------------------------------------------------- #
# Delegate to the local machine installer
# --------------------------------------------------------------------------- #
$bootstrap = Join-Path $RepoRoot 'bootstrap.ps1'
if (-not (Test-Path -LiteralPath $bootstrap)) {
    Write-Host "[perun] bootstrap.ps1 not found in $RepoRoot" -ForegroundColor Red
    exit 1
}

$args = @('-File', $bootstrap, '-Python', $pyExe)
if ($SkipDeps)   { $args += '-SkipDeps' }
if ($SkipGlobal) { $args += '-SkipGlobal' }
if ($SkipTask)   { $args += '-SkipTask' }
if ($NoIndex)    { $args += '-NoIndex' }
if ($NoShortcut) { $args += '-NoShortcut' }

Write-PerunStep "running machine installer ..."
& powershell.exe -ExecutionPolicy Bypass @args
$rc = $LASTEXITCODE
if ($rc -ne 0) {
    Write-Host "[perun] machine installer failed (exit code $rc)." -ForegroundColor Red
    exit $rc
}

Write-Host ""
Write-Host "  Perun is ready at $RepoRoot" -ForegroundColor Green
Write-Host "  Start it with: & '$RepoRoot\Perun.cmd'" -ForegroundColor Green
Write-Host ""
