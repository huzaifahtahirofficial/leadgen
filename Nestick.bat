@echo off
REM Nestick Tech Lead Generator - SkelerSecurity Intelligence Engine
REM Desktop launcher for Windows. Double-click to run.
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM --- locate Python 3.10+ ----------------------------------------------------
set "PY="
for %%C in (py python) do (
  if not defined PY (
    %%C -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if !errorlevel! == 0 set "PY=%%C"
  )
)

if not defined PY (
  echo Nestick needs Python 3.10 or newer.
  echo Install it from https://www.python.org/downloads/
  echo Tick "Add Python to PATH" during setup, then run this again.
  pause
  exit /b 1
)

REM --- private virtualenv -----------------------------------------------------
set "VENV=.nestick-venv"
if not exist "%VENV%" (
  echo First run: setting up ^(about 30 seconds^)...
  %PY% -m venv "%VENV%"
  if errorlevel 1 (
    echo Could not create a virtual environment.
    pause
    exit /b 1
  )
)

set "VPY=%VENV%\Scripts\python.exe"
"%VPY%" -c "import httpx" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies...
  "%VPY%" -m pip install --quiet --upgrade pip
  "%VPY%" -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed. Are you online?
    pause
    exit /b 1
  )
)

echo Starting Nestick Tech Lead Generator...
echo.
echo If a browser window does not appear, open the address printed below
echo manually. Keep this console window open while you use the app.
echo.
"%VPY%" -m nestick.desktop %*
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo Nestick exited with code %RC%.
  echo If you saw ERR_CONNECTION_REFUSED, try:  Nestick.bat --mode browser
)
pause
