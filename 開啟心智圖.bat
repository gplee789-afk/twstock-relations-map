@echo off
rem Double-click to run. First run creates .venv and installs packages (needs Python 3.10+).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: creating Python environment, please wait...
  py -3 -m venv .venv 2>/dev/null || python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Python 3.10 or newer was not found.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
  pause
  exit /b 1
)
if not exist ".venv\installed.ok" (
  echo Installing packages...
  ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo Package install failed. Check the network connection and run this again.
    pause
    exit /b 1
  )
  echo ok> ".venv\installed.ok"
)
".venv\Scripts\python.exe" start.py %*
pause
