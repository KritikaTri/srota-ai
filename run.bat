@echo off
REM ============================================================
REM SrotaAI - One-command startup for Windows (PowerShell or cmd)
REM Usage:  run.bat          start (or confirm running)
REM         run.bat stop     stop the server
REM         run.bat restart  restart
REM ============================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

if "%PORT%"=="" set PORT=8001
set PIDFILE=.server.pid
set LOGFILE=server.log
set URL=http://localhost:%PORT%

REM --- Find Python ---
where python >nul 2>nul
if %errorlevel% equ 0 (
    set PY=python
) else (
    where py >nul 2>nul
    if %errorlevel% equ 0 (
        set PY=py -3
    ) else (
        echo ERROR: Python not found. Install Python 3.11+ from https://python.org
        echo Make sure to check "Add Python to PATH" during install.
        pause
        exit /b 1
    )
)

if "%1"=="stop"    goto :stop
if "%1"=="restart" goto :restart
goto :start

:stop
if exist "%PIDFILE%" (
    set /p PID=<"%PIDFILE%"
    taskkill /PID !PID! /F >nul 2>nul
    del "%PIDFILE%" >nul 2>nul
    echo Stopped server.
) else (
    echo No PID file found.
)
REM Best-effort port cleanup
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>nul
)
exit /b 0

:restart
call :stop
timeout /t 1 /nobreak >nul

:start
REM --- Create venv if needed ---
if not exist ".venv" (
    echo Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo ERROR: failed to create venv. Make sure Python 3.11+ is installed.
        pause
        exit /b 1
    )
)

REM --- Activate venv ---
call .venv\Scripts\activate.bat

REM --- Install deps if missing ---
python -c "import fastapi" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies ^(one-time, ~30s^)...
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo ERROR: dependency install failed.
        pause
        exit /b 1
    )
)

REM --- Seed DB on first run ---
if not exist "srotaai.db" (
    if exist "srotaai_seed.db" (
        echo Initialising database from seed snapshot...
        copy /Y srotaai_seed.db srotaai.db >nul
    )
)

REM --- Start server in background ---
echo Starting SrotaAI on port %PORT%...
start /B "" cmd /c "python -m uvicorn srotaai.web.app:app --host 0.0.0.0 --port %PORT% --log-level info > %LOGFILE% 2>&1"

REM Save the PID of the python process (best effort)
timeout /t 2 /nobreak >nul
for /f "tokens=2" %%p in ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST ^| findstr PID') do (
    echo %%p > "%PIDFILE%"
    goto :wait
)

:wait
REM --- Wait up to 10s for readiness ---
set /a tries=0
:waitloop
set /a tries+=1
timeout /t 1 /nobreak >nul
curl -s -o NUL "http://127.0.0.1:%PORT%/" >nul 2>nul
if %errorlevel% equ 0 goto :ready
if %tries% lss 10 goto :waitloop

echo ERROR: Server did not become ready. Last 20 lines of %LOGFILE%:
powershell -Command "Get-Content '%LOGFILE%' -Tail 20"
exit /b 1

:ready
echo.
echo ============================================================
echo   SrotaAI is running
echo ============================================================
echo.
echo     Open in your browser:  %URL%
echo.
echo     stop:    run.bat stop
echo     restart: run.bat restart
echo     logs:    type %LOGFILE%
echo ============================================================
exit /b 0
