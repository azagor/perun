#requires -Version 5.1
<#
.SYNOPSIS
    Install (or repair) Perun on this machine.

.DESCRIPTION
    One command to bring Perun up on a fresh Windows machine:

      1. locate a Python 3.10+ interpreter   (PERUN_PYTHON overrides)
      2. install the Python dependencies     (requirements.txt)
      3. install Perun into the global OpenCode config
         (the 'perun' MCP server + the managed AGENTS.md block + slash-commands)
      4. optionally register the single-instance 'PerunBridge' logon task (Telegram)
      5. create a desktop shortcut to Perun.cmd

    By default the Telegram bridge starts with the dashboard (Perun.cmd) and is
    not registered as a logon task. Use -Task to opt into auto-start-at-logon.

    Everything after step 1 is delegated to 'python -m perun install --machine',
    so this script never duplicates logic; it just finds Python and hands over.
    It is idempotent — re-run it any time to repair the installation.

.PARAMETER Python
    Explicit path to python.exe. If omitted, PERUN_PYTHON is tried, then the
    'py -3' launcher, then 'python' on PATH.

.PARAMETER SkipDeps
    Do not run 'pip install -r requirements.txt'.

.PARAMETER SkipGlobal
    Do not touch the global OpenCode config (~/.config/opencode).

.PARAMETER SkipTask
    Do not register the PerunBridge scheduled task.

.PARAMETER NoIndex
    Do not (re)index project docs into the semantic-memory store.

.PARAMETER NoShortcut
    Do not create the desktop shortcut.

.EXAMPLE
    .\bootstrap.ps1

.EXAMPLE
    .\bootstrap.ps1 -Python C:\Python314\python.exe -NoShortcut
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$SkipDeps,
    [switch]$SkipGlobal,
    [switch]$SkipTask,
    [switch]$Task,
    [switch]$NoIndex,
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Stop'

$RepoRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location -LiteralPath $RepoRoot

if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'perun\__main__.py'))) {
    Write-Host "[perun] '$RepoRoot' does not look like the Perun repo (perun\__main__.py is missing)." -ForegroundColor Red
    exit 1
}

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

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Yellow
Write-Host "   PERUN - installer" -ForegroundColor Yellow
Write-Host "  ============================================================" -ForegroundColor Yellow

$pyExe = Resolve-PythonExe -Explicit $Python
if (-not $pyExe) {
    Write-Host "[perun] No Python 3.10+ found." -ForegroundColor Red
    Write-Host "        Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
    Write-Host "        or set PERUN_PYTHON to your python.exe path, then re-run this script."
    exit 1
}

# The scheduled task and Perun.cmd must use this exact interpreter.
$env:PERUN_PYTHON = $pyExe
$pyVer = ((& $pyExe -c "import sys; print(sys.version.split()[0])") | Select-Object -First 1)
Write-Host "[perun] python  : $pyExe ($pyVer)"
Write-Host "[perun] repo    : $RepoRoot"
Write-Host ""

$installArgs = @('-m', 'perun', 'install', '--machine')
if ($SkipDeps)   { $installArgs += '--skip-deps' }
if ($SkipGlobal) { $installArgs += '--skip-global' }
if ($SkipTask)   { $installArgs += '--skip-task' }
if ($Task)       { $installArgs += '--task' }
if ($NoIndex)    { $installArgs += '--no-index' }

& $pyExe @installArgs
$rc = $LASTEXITCODE
if ($rc -ne 0) {
    Write-Host "[perun] install failed (exit code $rc)." -ForegroundColor Red
    exit $rc
}

if (-not $NoShortcut) {
    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        $lnk = Join-Path $desktop 'Perun.lnk'
        $icon = Join-Path $RepoRoot 'web\assets\perun.ico'
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($lnk)
        $shortcut.TargetPath = Join-Path $RepoRoot 'Perun.cmd'
        $shortcut.WorkingDirectory = $RepoRoot
        $shortcut.Description = 'Perun - overseer & orchestrator'
        if (Test-Path -LiteralPath $icon) { $shortcut.IconLocation = $icon }
        $shortcut.Save()
        Write-Host "[perun] shortcut: $lnk"
    } catch {
        Write-Host "[perun] shortcut: skipped ($($_.Exception.Message))" -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host "  Done. Start Perun by double-clicking Perun.cmd (or the desktop icon)." -ForegroundColor Green
Write-Host "  Dashboard: python -m perun serve  ->  http://127.0.0.1:8760"
Write-Host ""
