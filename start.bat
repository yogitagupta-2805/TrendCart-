@echo off
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py
if "%PY%"=="" (
  where python >nul 2>nul && set PY=python
)
if "%PY%"=="" (
  echo Python is not installed. Download it from https://www.python.org/downloads/
  echo IMPORTANT: during install, tick "Add python.exe to PATH". Then run this file again.
  pause
  exit /b
)

if not exist venv\Scripts\python.exe (
  echo First time setup - installing, please wait...
  %PY% -m venv venv
  venv\Scripts\python.exe -m pip install -r requirements.txt
)

venv\Scripts\python.exe -m pip install -q -r requirements.txt
start "" cmd /c "timeout /t 3 >nul && start http://localhost:3000"
venv\Scripts\python.exe app.py
pause
