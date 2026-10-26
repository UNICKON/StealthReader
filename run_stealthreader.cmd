@echo off
rem StealthReader launcher (ASCII only). Picks a Python that can really run us.
rem 2026-10-26: %APPDATA%\WPS*\python-env got replaced by a half-extracted tree
rem (python312.dll and pythonw.exe both gone), so "the exe exists" proves nothing and
rem even the exit code lies - a broken interpreter dies with 0xC0000135, which cmd sees
rem as a negative number and never flags. So each candidate must WRITE A MARKER FILE
rem after importing PyQt5 + requests; only then is it used. pythonw.exe is a GUI app,
rem cmd does not wait for it, hence the start /wait branch.
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%libs"
set "PYW="
for /d %%d in ("%APPDATA%\WPS*") do call :try "%%d\python-env\pythonw.exe"
for /d %%d in ("%APPDATA%\WPS*") do call :try "%%d\python-env\python.exe"
rem Any app under %LOCALAPPDATA%\Programs that ships an "office runtime" (the DSH family
rem does) can host us - scanned by name so a future rename does not break the launcher.
for /d %%d in ("%LOCALAPPDATA%\Programs\*") do call :try "%%d\resources\office-runtime\primary-runtime\dependencies\python\pythonw.exe"
for /d %%d in ("%LOCALAPPDATA%\Programs\*") do call :try "%%d\resources\office-runtime\primary-runtime\dependencies\python\python.exe"
for /f "delims=" %%p in ('where pythonw 2^>nul') do call :try "%%p"
for /f "delims=" %%p in ('where python 2^>nul') do call :try "%%p"
if not defined PYW (
  echo [StealthReader] No working Python found - needs PyQt5 + requests.
  echo   Looked at: %%APPDATA%%\WPS*\python-env and pythonw/python on PATH.
  pause
  exit /b 1
)
cd /d "%HERE%"
set "STEALTHREADER_PYW=%PYW%"
echo [StealthReader] using %PYW%
rem extra args are forwarded: --hidden, --restart-wait <pid>, ...
start "" "%PYW%" main.py %*
endlocal
exit /b 0

:try
if defined PYW goto :eof
if not exist "%~1" goto :eof
set "SRT=%TEMP%\sr_pytest_%RANDOM%%RANDOM%.ok"
del /q "%SRT%" >nul 2>&1
echo %~nx1 | findstr /i "pythonw.exe" >nul
if errorlevel 1 (
  "%~1" -c "import PyQt5.QtWidgets, requests; open(r'%SRT%','w').close()" >nul 2>&1
) else (
  start "" /wait "%~1" -c "import PyQt5.QtWidgets, requests; open(r'%SRT%','w').close()" >nul 2>&1
)
if exist "%SRT%" set "PYW=%~1"
del /q "%SRT%" >nul 2>&1
goto :eof
