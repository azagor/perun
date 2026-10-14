@echo off
setlocal EnableExtensions
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
rem  with HTTP 409s. Two independent guards prevent that:
rem    1. this launcher NEVER runs `perun bridge` itself;
rem    2. the bridge holds a machine-global named mutex (PidLock), and the
rem       PerunBridge logon task is registered with "IgnoreNew" (a second
rem       trigger is refused). `schtasks /run` therefore just ENSURES it is up.
set "BRIDGE=not running"
schtasks /query /tn "PerunBridge" >nul 2>nul
if not errorlevel 1 (
    schtasks /run /tn "PerunBridge" >nul 2>nul
    set "BRIDGE=running (single instance, PerunBridge task)"
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

%PY% -m perun serve --port %PORT%
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    echo.
    echo [Perun] Server exited with code %RC%.
    pause
)
endlocal
