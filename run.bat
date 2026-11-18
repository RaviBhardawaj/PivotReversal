@echo off
rem Pivot Reversal - start the Glass dashboard on Windows.
rem   run.bat            -> http://127.0.0.1:8780
rem First run: makes .venv, installs requirements.txt and creates .env from .env.example.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up .venv ...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Python 3.10 or newer is needed: https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
    pause & exit /b 1
  )
)
if not exist ".venv\.req_ok" (
  echo Installing packages from requirements.txt ...
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo Package install failed. & pause & exit /b 1)
  type nul > ".venv\.req_ok"
)
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo.
  echo Created .env from .env.example. Put your Angel One keys in it:
  echo     %cd%\.env
  echo then run run.bat again.
  notepad ".env"
  pause & exit /b 1
)
echo Glass dashboard: http://127.0.0.1:8780/   (Ctrl+C to stop)
:loop
".venv\Scripts\python.exe" glass_server.py
if %errorlevel%==0 goto :eof
echo Dashboard exited, restarting in 5 s (Ctrl+C to stop)
set GLASS_NO_BROWSER=1
timeout /t 5 /nobreak >nul
goto loop
