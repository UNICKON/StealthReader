@echo off
rem StealthReader launcher (ASCII only). Uses the WPS Lingxi Python + libs in this folder.
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%libs"
set "PYW="
for /d %%d in ("%APPDATA%\WPS*") do if exist "%%d\python-env\pythonw.exe" set "PYW=%%d\python-env\pythonw.exe"
if not defined PYW for /d %%d in ("%APPDATA%\WPS*") do if exist "%%d\python-env\python.exe" set "PYW=%%d\python-env\python.exe"
if not defined PYW (
  echo [StealthReader] Python environment not found under %APPDATA%\WPS*
  pause
  exit /b 1
)
cd /d "%HERE%"
start "" "%PYW%" main.py
endlocal
