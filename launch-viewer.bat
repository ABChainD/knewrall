@echo off
setlocal enabledelayedexpansion
REM Single-command launcher for the Knewrall 3D Graph Viewer: sets up the
REM backend venv and frontend build on first run (idempotent -- skips
REM anything already present), starts the server, and opens it in the
REM default browser. Double-click this file, or run from a terminal.
REM
REM Usage:
REM   launch-viewer.bat              start (or reuse an already-running server)
REM   launch-viewer.bat --rebuild    also force a fresh "npm run build"
REM                                  (use after editing frontend\src\**)
REM   launch-viewer.bat --stop       stop a server started by this script
REM                                  (reads the pidfile)

set "ROOT=%~dp0"
set "VIEWER_DIR=%ROOT%viewer"
if not defined KNEWRALL_VIEWER_PORT set "KNEWRALL_VIEWER_PORT=8798"
set "PORT=%KNEWRALL_VIEWER_PORT%"
set "URL=http://127.0.0.1:%PORT%"
set "PID_FILE=%VIEWER_DIR%\.viewer.pid"

if "%~1"=="--stop" (
    if exist "%PID_FILE%" (
        set /p STOP_PID=<"%PID_FILE%"
        echo [knewrall-viewer] Stopping server ^(pid !STOP_PID!^)...
        taskkill /PID !STOP_PID! /T /F >nul 2>nul
        del /f /q "%PID_FILE%" >nul 2>nul
        echo [knewrall-viewer] Stopped.
    ) else (
        echo [knewrall-viewer] Not running ^(no %PID_FILE%^).
    )
    endlocal
    exit /b 0
)

set "REBUILD=0"
if "%~1"=="--rebuild" set "REBUILD=1"

cd /d "%VIEWER_DIR%" || (echo [knewrall-viewer] Could not find %VIEWER_DIR% & exit /b 1)

REM Retries (not a single shot): a lone curl attempt can hit a transient
REM hiccup and falsely report "not running," which would let a second
REM scripts\run.py start, briefly clobber the pidfile with its own pid while
REM it fails to bind the port, then delete it on exit -- orphaning the
REM first, genuinely-running server's pidfile (this happened during testing;
REM run.py itself now also guards against it, but this avoids the race
REM entirely).
set "ALREADY_UP=1"
for /l %%i in (1,1,3) do (
    curl -s -o nul --max-time 1 "%URL%/api/health" 2>nul && (set "ALREADY_UP=0" & goto :checked)
    ping -n 1 127.0.0.1 >nul
)
:checked
if "%ALREADY_UP%"=="0" (
    echo [knewrall-viewer] Already running at %URL% -- opening browser.
    start "" "%URL%"
    endlocal
    exit /b 0
)

REM -- Python venv (isolated from any shared/global Python env -- see README) --
if not exist ".venv\Scripts\python.exe" (
    echo [knewrall-viewer] Creating backend venv...
    python -m venv .venv || (echo [knewrall-viewer] python not found on PATH & exit /b 1)
)
set "VENV_PY=%VIEWER_DIR%\.venv\Scripts\python.exe"

"%VENV_PY%" -c "import fastapi, uvicorn" 1>nul 2>nul
if errorlevel 1 (
    echo [knewrall-viewer] Installing backend dependencies...
    "%VENV_PY%" -m pip install --quiet -r requirements.txt -r ..\requirements.txt
)

REM -- Frontend build (skips if already built; --rebuild forces it) --
if not exist "frontend\node_modules" (
    echo [knewrall-viewer] Installing frontend dependencies ^(npm install^)...
    pushd frontend && call npm install && popd
)
if "%REBUILD%"=="1" (
    echo [knewrall-viewer] Building frontend...
    pushd frontend && call npm run build && popd
) else if not exist "backend\static\index.html" (
    echo [knewrall-viewer] Building frontend...
    pushd frontend && call npm run build && popd
)

REM -- Start the server in its own window, then open the browser once it's up --
del /f /q "%PID_FILE%" >nul 2>nul
echo [knewrall-viewer] Starting server on %URL% ...
start "Knewrall Viewer" /D "%VIEWER_DIR%" "%VENV_PY%" scripts\run.py

REM "timeout /nobreak" errors out ("Input redirection is not supported")
REM whenever stdin is redirected/piped instead of a real console -- which
REM silently breaks the wait (each loop iteration fails instantly instead of
REM pausing, so the browser can open before the server is ready). "ping" as
REM a delay doesn't depend on console/stdin state at all, so it's reliable
REM here regardless of how this script is invoked.
set "READY=0"
for /l %%i in (1,1,30) do (
    curl -s -o nul --max-time 1 "%URL%/api/health" 2>nul && (set "READY=1" & goto :ready)
    ping -n 2 127.0.0.1 >nul
)
:ready
start "" "%URL%"

echo [knewrall-viewer] Running at %URL% in a separate window.
echo [knewrall-viewer] Close that window, Ctrl+C in it, or run "launch-viewer.bat --stop" to stop the server.
endlocal
