@echo off
rem Run Reelarr on Windows without Docker.
rem   run.bat            first run: makes a private Python environment in .venv
rem                      and installs what Reelarr needs, then starts it
rem   git pull, run.bat  update: re-installs only if requirements changed
rem Settings, databases and logs live in %APPDATA%\Reelarr, never here.
setlocal
cd /d "%~dp0"

set "PY=py -3"
where py >nul 2>nul || set "PY=python"
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>nul
if errorlevel 1 (
    echo Reelarr needs Python 3.10 or newer: https://www.python.org/downloads/
    exit /b 1
)
where ffmpeg >nul 2>nul || echo warning: ffmpeg not found - Reelarr can't read or convert audio without it ^(winget install ffmpeg^)

if not exist .venv\Scripts\python.exe (
    echo Setting up Reelarr's Python environment in .venv ^(one time^)...
    %PY% -m venv .venv || exit /b 1
)
set "STAMP=.venv\.requirements"
for %%F in (requirements.txt) do set "WANT=%%~zF-%%~tF"
set "HAVE="
if exist "%STAMP%" set /p HAVE=<"%STAMP%"
if not "%HAVE%"=="%WANT%" (
    .venv\Scripts\python -m pip install --quiet --upgrade pip
    .venv\Scripts\python -m pip install --quiet -r requirements.txt || exit /b 1
    > "%STAMP%" echo %WANT%
)

.venv\Scripts\python -m app %*
