#requires -Version 5.1
<#
.SYNOPSIS
    Install Perun prerequisites and finish Perun machine setup.

.DESCRIPTION
    Called by the Inno Setup installer after files are extracted.
    It can install Python, git, the OpenCode CLI, and Ollama if they are
    missing, then install Perun's Python dependencies and run the machine
    installer.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$PerunDir,

    [switch]$InstallPython,
    [switch]$InstallGit,
    [switch]$InstallOpencode,
    [switch]$InstallOllama,
    [switch]$SkipMachineInstall
)

$ErrorActionPreference = 'Stop'

$StateFile = Join-Path $PerunDir '.perun-installer-state.json'
$LogFile   = Join-Path $PerunDir 'installer.log'

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] [$Level] $Message"
    Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue
    if ($Level -eq 'ERROR') { Write-Host $line -ForegroundColor Red }
    elseif ($Level -eq 'WARN') { Write-Host $line -ForegroundColor DarkYellow }
    else { Write-Host $line }
}

$state = @{
    perun_dir = $PerunDir
    python = ''
    git = ''
    opencode = ''
    ollama = ''
}

# SHA256 hashes for installer binaries downloaded by this script.
# Update these whenever the download URL or version changes.
$DownloadHashes = @{
    'python-3.12.10-amd64.exe' = '67B5635E80EA51072B87941312D00EC8927C4DB9BA18938F7AD2D27B328B95FB'
    'Git-2.56.0.2-64-bit.exe'  = '52188F917B378F00C70EC136BCF090005F30D44FBC4EBA0BCE759CC6592D60F6'
    'opencode-windows-x64.zip' = 'E4CE9117F55FC068414513789F8C7D1049F855D7518BB4E350B9170A3A20D8C3'
    'OllamaSetup-v0.40.5.exe'  = 'EA0B50685D382BC0AF164ABDBF9B41BD89ABB98DE14FC51E4890A7176757A519'
}

function Assert-InstallerHash {
    param([string]$FilePath)
    $name = Split-Path -Leaf $FilePath
    $expected = $DownloadHashes[$name]
    if (-not $expected) { return }
    if ($expected -eq 'TBD') {
        Write-Log "No SHA256 recorded for $name; skipping verification" -Level 'WARN'
        return
    }
    $actual = (Get-FileHash -Path $FilePath -Algorithm SHA256).Hash
    if ($actual -ne $expected) {
        throw "SHA256 mismatch for $name`: expected $expected, got $actual"
    }
    Write-Log "SHA256 verified for $name"
}

function Save-State {
    $state | ConvertTo-Json -Depth 4 | Set-Content -Path $StateFile -Encoding UTF8
}

function Add-ToPath {
    param([string]$Dir, [string]$Scope = 'Machine')
    if (-not (Test-Path -LiteralPath $Dir)) { return }
    $key = if ($Scope -eq 'Machine') { 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' } else { 'HKCU:\Environment' }
    $current = (Get-ItemProperty -Path $key -Name 'Path' -ErrorAction SilentlyContinue).Path
    if ($current -like "*$Dir*") { return }
    $new = "$Dir;$current"
    Set-ItemProperty -Path $key -Name 'Path' -Value $new
    # Refresh the current process environment.
    [Environment]::SetEnvironmentVariable('Path', $new, $Scope)
}

function Set-EnvVar {
    param([string]$Name, [string]$Value, [string]$Scope = 'Machine')
    $key = if ($Scope -eq 'Machine') { 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' } else { 'HKCU:\Environment' }
    Set-ItemProperty -Path $key -Name $Name -Value $Value
    [Environment]::SetEnvironmentVariable($Name, $Value, $Scope)
}

function Get-EnvVar {
    param([string]$Name, [string]$Scope = 'Machine')
    $key = if ($Scope -eq 'Machine') { 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' } else { 'HKCU:\Environment' }
    (Get-ItemProperty -Path $key -Name $Name -ErrorAction SilentlyContinue).$Name
}

function Find-OnPath {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return ''
}

function Test-Python {
    $py = Find-OnPath 'python'
    if (-not $py) { return '' }
    try {
        $ver = & $py -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($ver -and [version]$ver -ge [version]'3.10') { return $py }
    } catch {}
    return ''
}

function Install-Python {
    $url = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
    $installer = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'

    Write-Log "Downloading Python 3.12.10 ..."
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    Assert-InstallerHash -FilePath $installer

    Write-Log "Installing Python (all users, default location) ..."
    $args = @(
        '/quiet'
        'InstallAllUsers=1'
        'PrependPath=1'
        'Include_test=0'
        'Include_launcher=0'
    )
    $proc = Start-Process -FilePath $installer -ArgumentList $args -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -ne 0) { throw "Python installer exited with code $($proc.ExitCode)" }

    # Default install location for Python 3.12 all-users is predictable.
    $py = 'C:\Program Files\Python312\python.exe'
    if (-not (Test-Path -LiteralPath $py)) {
        # Allow for a different default directory by searching PATH.
        $py = Find-OnPath 'python'
        if (-not $py) { throw "python.exe not found after install" }
    }
    Set-EnvVar -Name 'PERUN_PYTHON' -Value $py -Scope 'Machine'
    $state.python = $py
    Write-Log "Python installed at $py"
}

function Test-Git {
    return Find-OnPath 'git'
}

function Install-Git {
    $target = Join-Path $PerunDir 'Git'
    $url = 'https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.2/Git-2.56.0.2-64-bit.exe'
    $installer = Join-Path $env:TEMP 'Git-2.56.0.2-64-bit.exe'

    Write-Log "Downloading Git for Windows ..."
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    Assert-InstallerHash -FilePath $installer

    Write-Log "Installing Git to $target ..."
    $args = @(
        '/VERYSILENT'
        '/NORESTART'
        '/NOCANCEL'
        '/SP-'
        "/DIR=`"$target`""
    )
    $proc = Start-Process -FilePath $installer -ArgumentList $args -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -ne 0) { throw "Git installer exited with code $($proc.ExitCode)" }

    $git = Join-Path $target 'bin\git.exe'
    if (-not (Test-Path -LiteralPath $git)) { throw "git.exe not found after install" }
    Add-ToPath -Dir (Join-Path $target 'bin') -Scope 'Machine'
    Add-ToPath -Dir (Join-Path $target 'cmd') -Scope 'Machine'
    $state.git = $git
    Write-Log "Git installed at $git"
}

function Test-Opencode {
    return Find-OnPath 'opencode'
}

function Install-Opencode {
    # Prefer the official npm package if npm is available.
    $npm = Find-OnPath 'npm'
    if ($npm) {
        Write-Log "Installing OpenCode CLI via npm ..."
        & $npm install -g opencode-ai
        if ($LASTEXITCODE -ne 0) { throw "npm install -g opencode-ai failed" }
        $oc = Find-OnPath 'opencode'
        if ($oc) {
            $state.opencode = $oc
            Write-Log "OpenCode CLI installed at $oc"
            return
        }
    }

    # Fallback to the standalone Windows binary.
    $target = Join-Path $PerunDir 'OpenCode'
    $url = 'https://github.com/anomalyco/opencode/releases/download/v1.19.3/opencode-windows-x64.zip'
    $zip = Join-Path $env:TEMP 'opencode-windows-x64.zip'

    Write-Log "Downloading OpenCode CLI standalone binary ..."
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Assert-InstallerHash -FilePath $zip

    Write-Log "Extracting OpenCode CLI to $target ..."
    if (Test-Path -LiteralPath $target) { Remove-Item -Recurse -Force -LiteralPath $target }
    Expand-Archive -Path $zip -DestinationPath $target -Force

    $exe = Get-ChildItem -Path $target -Filter 'opencode.exe' -Recurse | Select-Object -First 1
    if (-not $exe) { throw "opencode.exe not found after extraction" }
    Add-ToPath -Dir $exe.DirectoryName -Scope 'Machine'
    $state.opencode = $exe.FullName
    Write-Log "OpenCode CLI installed at $($exe.FullName)"
}

function Test-Ollama {
    return Find-OnPath 'ollama'
}

function Install-Ollama {
    $winget = Find-OnPath 'winget'
    if ($winget) {
        Write-Log "Installing Ollama via winget ..."
        $args = @('install', '-e', '--id', 'Ollama.Ollama', '--accept-source-agreements', '--accept-package-agreements', '--silent')
        $proc = Start-Process -FilePath $winget -ArgumentList $args -Wait -PassThru -NoNewWindow
        if ($proc.ExitCode -eq 0) {
            $ollama = Find-OnPath 'ollama'
            if ($ollama) {
                $state.ollama = $ollama
                Write-Log "Ollama installed at $ollama"
                return
            }
        }
        Write-Log "winget install of Ollama failed or ollama not on PATH; falling back to installer." -Level 'WARN'
    }

    $url = 'https://github.com/ollama/ollama/releases/download/v0.40.5/OllamaSetup.exe'
    $installer = Join-Path $env:TEMP 'OllamaSetup-v0.40.5.exe'

    Write-Log "Downloading Ollama installer ..."
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    Assert-InstallerHash -FilePath $installer

    Write-Log "Installing Ollama ..."
    $args = @('/VERYSILENT', '/NORESTART', '/NOCANCEL')
    $proc = Start-Process -FilePath $installer -ArgumentList $args -Wait -PassThru -NoNewWindow
    if ($proc.ExitCode -ne 0) { throw "Ollama installer exited with code $($proc.ExitCode)" }

    $ollama = Find-OnPath 'ollama'
    if (-not $ollama) { throw "ollama.exe not found after install" }
    $state.ollama = $ollama
    Write-Log "Ollama installed at $ollama"
}

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
Write-Log "Starting Perun prerequisite install in $PerunDir"

$py = Test-Python
if ($py) {
    Write-Log "Python already available: $py"
    $state.python = $py
    Set-EnvVar -Name 'PERUN_PYTHON' -Value $py -Scope 'Machine'
} elseif ($InstallPython) {
    Install-Python
} else {
    Write-Log "Python not found and -InstallPython not set; skipping Python install" -Level 'WARN'
}

$git = Test-Git
if ($git) {
    Write-Log "Git already available: $git"
    $state.git = $git
} elseif ($InstallGit) {
    Install-Git
} else {
    Write-Log "Git not found and -InstallGit not set; skipping Git install" -Level 'WARN'
}

$oc = Test-Opencode
if ($oc) {
    Write-Log "OpenCode CLI already available: $oc"
    $state.opencode = $oc
} elseif ($InstallOpencode) {
    Install-Opencode
} else {
    Write-Log "OpenCode CLI not found and -InstallOpencode not set; skipping" -Level 'WARN'
}

$ollama = Test-Ollama
if ($ollama) {
    Write-Log "Ollama already available: $ollama"
    $state.ollama = $ollama
} elseif ($InstallOllama) {
    Install-Ollama
} else {
    Write-Log "Ollama not found and -InstallOllama not set; skipping" -Level 'WARN'
}

Save-State

if ($SkipMachineInstall) {
    Write-Log 'Skipping Perun machine install.'
    exit 0
}

$pythonExe = $state.python
if (-not $pythonExe) {
    # Try to resolve from PATH or PERUN_PYTHON after refresh.
    $pythonExe = Test-Python
    if (-not $pythonExe) {
        Write-Log "No usable Python found; cannot finish Perun install." -Level 'ERROR'
        exit 1
    }
}

Write-Log "Installing Python dependencies with $pythonExe ..."
& $pythonExe -m pip install -r (Join-Path $PerunDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    Write-Log "pip install failed with exit code $LASTEXITCODE" -Level 'ERROR'
    exit 1
}

Write-Log "Running Perun machine installer ..."
Push-Location -LiteralPath $PerunDir
& $pythonExe -m perun install --machine
$rc = $LASTEXITCODE
Pop-Location

if ($rc -ne 0) {
    Write-Log "Perun machine install failed with exit code $rc" -Level 'ERROR'
    exit $rc
}

Write-Log 'Perun installation completed successfully.'
