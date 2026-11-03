@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Perun - Overseer

rem =====================================================================
rem  Perun - one-click launcher
rem ---------------------------------------------------------------------
rem  Double-click this file to bring Perun up:
rem    * starts the dashboard + API  (http://127.0.0.1:8760)
rem    * opens it in your browser
rem    * starts the Telegram bridge in-process (only while the dashboard runs)
rem
rem  Close this window (or press Ctrl+C) to stop the dashboard.
rem
rem  Optional overrides (set before running, or edit below):
rem    PERUN_PYTHON=<path to python.exe>   pick a specific interpreter
rem    PERUN_PORT=<n>                      dashboard port (default: config/8760)
rem    PERUN_NO_BROWSER=1                  do not open a browser
rem    PERUN_FORCE_BRIDGE=1                always start bridge in dashboard (debug)
rem    PERUN_PASSWORD=<password>           decrypt encrypted config values
rem    PERUN_SKIP_DEP_CHECK=1              do not verify/repair Python packages
rem =====================================================================

set "PERUN_DIR=%~dp0"
if "%PERUN_DIR:~-1%"=="\" set "PERUN_DIR=%PERUN_DIR:~0,-1%"
set "PYTHONPATH=%PERUN_DIR%;%PYTHONPATH%"
cd /d "%PERUN_DIR%"

rem -- find a Python 3 -------------------------------------------------
set "PY="
if defined PERUN_PYTHON set "PY=%PERUN_PYTHON%"
if not defined PY if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
    echo [Perun] Could not find Python 3.10+.
    echo         Install Python, or set PERUN_PYTHON to your python.exe path.
    echo.
    pause
    exit /b 1
)

rem -- normalize PY for safe execution (quote paths with spaces) --
if defined PY (
    if /I not "%PY%"=="py" (
        if /I not "%PY:~0,3%"=="py " (
            set "PY_NOQ=%PY:"=%"
            if "!PY_NOQ!"=="%PY%" (
                set "PY_NOSP=%PY: =%"
                if not "!PY_NOSP!"=="%PY%" (
                    set "PY="%PY%""
                )
                set "PY_NOSP="
            )
            set "PY_NOQ="
        )
    )
)

rem -- Python dependencies: verify, repair once, then explain -------------
rem  A machine-wide install lives under "%ProgramFiles%\Perun" and runs the
rem  system Python, so a skipped/failed `pip install` during setup leaves a
rem  half-working service (typically `ModuleNotFoundError: psutil`). Check the
rem  installed distributions and repair them here instead of crashing later.
if not defined PERUN_SKIP_DEP_CHECK (
    %PY% -c "import importlib.metadata as md,sys; ns='fastapi uvicorn starlette httpx fastmcp numpy pydantic psutil PyYAML cryptography croniter tzdata'.split(); miss=[n for n in ns if not any(d.metadata.get('Name','').lower()==n.lower() for d in md.distributions(name=n))]; (print('[Perun] Missing Python packages: ' + ', '.join(miss)) if miss else None); sys.exit(1 if miss else 0)"
    if errorlevel 1 (
        echo [Perun] Installing Python dependencies into %PY% ...
        echo [Perun] This can take a few minutes on the first run.
        %PY% -m pip install --quiet --no-input --disable-pip-version-check -r "%PERUN_DIR%\requirements.txt"
        if errorlevel 1 (
            echo [Perun] The system install failed; retrying for the current user ...
            %PY% -m pip install --quiet --no-input --user --disable-pip-version-check -r "%PERUN_DIR%\requirements.txt"
        )
        %PY% -c "import importlib.metadata as md,sys; ns='fastapi uvicorn starlette httpx fastmcp numpy pydantic psutil PyYAML cryptography croniter tzdata'.split(); sys.exit(0 if all(any(d.metadata.get('Name','').lower()==n.lower() for d in md.distributions(name=n)) for n in ns) else 1)" >nul 2>nul
        if errorlevel 1 (
            echo.
            echo [Perun] Required Python packages are still missing.
            echo         Run this once in a terminal - as administrator if Perun is
            echo         installed under Program Files - and then start Perun again:
            echo.
            echo             %PY% -m pip install -r "%PERUN_DIR%\requirements.txt"
            echo.
            echo         If that interpreter path is wrong, set PERUN_PYTHON to your
            echo         python.exe and run this launcher again.
            echo.
            pause
            exit /b 1
        )
        echo [Perun] Dependencies installed.
    )
)

rem -- resolve the dashboard port (Settings.port, default 8760) ---------
set "PORT=8760"
set "CFG=%USERPROFILE%\.perun\config.json"
if defined PERUN_HOME set "CFG=%PERUN_HOME%\config.json"
if defined PERUN_PORT (
    set "PORT=%PERUN_PORT%"
) else if exist "%CFG%" for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$c=Get-Content -Raw '%CFG%' -ErrorAction SilentlyContinue; if($c){ (ConvertFrom-Json $c).port }" 2^>nul`) do if not "%%p"=="" set "PORT=%%p"
set "URL=http://127.0.0.1:%PORT%"

rem -- is the dashboard already up? then just show it -------------------
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [Perun] Dashboard already running - opening %URL%
    if not defined PERUN_NO_BROWSER start "" "%URL%"
    exit /b 0
)

rem -- Telegram: the bridge is a SINGLETON, never started more than once --
rem  `getUpdates` allows one consumer per token, so two pollers would fight
rem  with HTTP 409s. The dashboard process owns the bridge by default
rem  (`--bridge`), guarded by a machine-global named mutex (PidLock). There is
rem  no separate auto-start logon task by default; the bridge is only active
rem  while the dashboard is running. Use `python -m perun install --machine --task`
rem  to opt into a PerunBridge logon task.
set "BRIDGE=in dashboard process"

rem  Ask Perun itself whether the bridge mutex is currently held. This is the
rem  definitive check: it is true only when a live process (the PerunBridge
rem  task, if registered, or another dashboard) is actually polling Telegram.
rem  A stale/crashed task will report "bridge_running: false" even if schtasks
rem  claims it is "Running", so we always fall back to starting the bridge in-process.
rem
rem  NOTE: never wrap %PY% in a `for /f` backtick command here. cmd strips the
rem  outer quotes of such a command line, so an interpreter at
rem  "C:\Program Files\Python312\python.exe" would run as `C:\Program`. A
rem  direct call preserves the quotes; the exit code carries the answer.
set "BRIDGE_ALREADY_UP=0"
if not defined PERUN_FORCE_BRIDGE (
    %PY% -c "import sys; from perun import config; from perun.telegram import PidLock; sys.exit(0 if PidLock.is_held(config.HOME / 'telegram.pid') else 1)" >nul 2>nul
    if not errorlevel 1 set "BRIDGE_ALREADY_UP=1"
)
if "%BRIDGE_ALREADY_UP%"=="1" (
    set "BRIDGE=running (detected live bridge)"
    set "BRIDGE_ARGS="
) else (
    set "BRIDGE_ARGS=--bridge"
)

echo.
echo  ---------------------------------------------------------------
echo   PERUN - Overseer
echo  ---------------------------------------------------------------
echo   Dashboard : %URL%
echo   Telegram  : %BRIDGE%
echo   Repo      : %PERUN_DIR%
echo   Python    : %PY%
echo  ---------------------------------------------------------------
echo   Close this window (or Ctrl+C) to stop the dashboard.
echo  ---------------------------------------------------------------
echo.

rem -- open the dashboard once the server is listening ------------------
if not defined PERUN_NO_BROWSER start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%URL%'" 1>nul 2>nul

:serve
if not defined PERUN_PASSWORD (
    rem  If config contains encrypted values, the dashboard will prompt for the
    rem  password once it is running. The bridge degrades gracefully until then.
    rem  Direct call (see the quoting note above) - never a `for /f` backtick.
    %PY% -c "import sys; from perun import config, secrets; sys.exit(0 if secrets.is_encrypted(config.Settings.load().telegram_token or '') else 1)" >nul 2>nul
    if not errorlevel 1 echo [Perun] Encrypted Telegram token found. The dashboard will ask for the password.
)
%PY% -m perun serve --port %PORT% %BRIDGE_ARGS%
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo [Perun] Server exited with code %RC%.
    pause
)
endlocal
