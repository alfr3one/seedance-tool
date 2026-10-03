@echo off
setlocal
title Dola Render Gateway
rem One-click local run: the first launch creates .venv and installs the Python
rem dependencies and Patchright Chromium; later launches just start the server.
rem Usage: start-windows.bat [port]   (default 8000)
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"
set "VENV_PY=.venv\Scripts\python.exe"
rem Keep returned video links on the port actually served.
set "DOLA_PORT=%PORT%"
set "PYTHONIOENCODING=utf-8"

if exist "%VENV_PY%" goto deps
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul || goto nopython
echo Creating virtual environment .venv ...
%PY% -m venv .venv || goto fail

:deps
rem Reinstall only when requirements.txt changed since the last successful install.
fc /b requirements.txt .venv\requirements.installed >nul 2>nul
if errorlevel 1 (
  echo Installing Python dependencies, the first run can take a few minutes...
  "%VENV_PY%" -m pip install -r requirements.txt || goto fail
  copy /y requirements.txt .venv\requirements.installed >nul
)
"%VENV_PY%" -m patchright install chromium || goto fail

rem No private proxy by default, so Chromium uses the normal/system connection.
rem A proxy can be enabled later in the dashboard Settings tab.
if not exist ".env.local" (
  echo DOLA_PROXY=>".env.local"
)

echo.
echo Dashboard: http://127.0.0.1:%PORT%/
echo Keep this window open while working. Press Ctrl+C to stop the server.
echo.
start "" powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 90;$i++){try{Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%PORT%/health' | Out-Null; Start-Process 'http://127.0.0.1:%PORT%/'; break}catch{Start-Sleep 1}}"
"%VENV_PY%" -m uvicorn server:app --host 127.0.0.1 --port %PORT%
echo.
echo The server stopped. If that was unexpected, read the messages above.
echo If port %PORT% is busy, run: start-windows.bat 8001
pause
exit /b

:nopython
echo [ERROR] Python 3.10 or newer was not found.
echo Install Python 3.12 from https://www.python.org/downloads/windows/
echo and tick "Add python.exe to PATH" on the first installer screen.
pause
exit /b 1

:fail
echo.
echo [ERROR] Setup failed, read the messages above.
echo With a brand-new Python release, install Python 3.12 instead,
echo delete the .venv folder and run this file again.
pause
exit /b 1
