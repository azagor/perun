@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Perun - Overseer

rem =====================================================================
rem  Perun - one-click launcher
rem ---------------------------------------------------------------------
rem  Double-click this file to bring Perun up:
rem    * starts the dashboard + API  (http://127.0.0.1:8760)
rem    * opens it in your browser
rem    * makes sure the Telegram bridge is running EXACTLY ONCE
rem
rem  Close this window (or press Ctrl+C) to stop the dashboard.
rem
rem  Optional overrides (set before running, or edit below):
rem    PERUN_PYTHON=<path to python.exe>   pick a specific interpreter
rem    PERUN_PORT=<n>                      dashboard port (default: config/8760)
rem    PERUN_NO_BROWSER=1                  do not open a browser
rem    PERUN_FORCE_BRIDGE=1                always start bridge in dashboard (debug)
rem    PERUN_PASSWORD=<password>           decrypt encrypted config values
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

rem -- resolve the dashboard port (Settings.port, default 8760) ---------
set "PORT=8760"
set "CFG=%USERPROFILE%\.perun\config.json"
if defined PERUN_HOME set "CFG=%PERUN_HOME%\config.json"
if defined PERUN_PORT set "PORT=%PERUN_PORT%"
if exist "%CFG%" for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$c=Get-Content -Raw '%CFG%' -ErrorAction SilentlyContinue; if($c){ (ConvertFrom-Json $c).port }" 2^>nul`) do if not "%%p"=="" set "PORT=%%p"
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
rem  with HTTP 409s. The dashboard process now owns the bridge by default
rem  (`--bridge`), guarded by a machine-global named mutex (PidLock). The
rem  separate PerunBridge logon task still exists for auto-start-at-logon,
rem  but this launcher no longer depends on it being healthy.
set "BRIDGE=in dashboard process"

rem  Ask Perun itself whether the bridge mutex is currently held. This is the
rem  definitive check: it is true only when a live process (the PerunBridge
rem  task or another dashboard) is actually polling Telegram. A stale/crashed
rem  task will report "bridge_running: false" even if schtasks claims it is
rem  "Running", so we always fall back to starting the bridge in-process.
set "BRIDGE_ALREADY_UP=0"
if not defined PERUN_FORCE_BRIDGE (
    for /f "usebackq delims=" %%j in (`%PY% -m perun status --json 2^>nul ^| powershell -NoProfile -Command "$in=$input -join '\n'; if($in -match 'bridge_running\s*:\s*true'){ '1' } else { '0' }"`) do (
        set "BRIDGE_ALREADY_UP=%%j"
    )
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
    for /f "usebackq delims=" %%e in (`%PY% -c "from perun import config,secrets; print('1' if secrets.is_encrypted(config.Settings.load().telegram_token or '') else '0')" 2^>nul`) do (
        if "%%e"=="1" (
            echo [Perun] Encrypted Telegram token found. The dashboard will ask for the password.
        )
    )
)
%PY% -m perun serve --port %PORT% %BRIDGE_ARGS%
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo [Perun] Server exited with code %RC%.
    pause
)
endlocal
