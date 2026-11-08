@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "SE_CACHE_PATH=%~dp0driver-cache"
set "NO_PROXY=localhost,127.0.0.1,%NO_PROXY%"

if not exist ".venv\Scripts\python.exe" (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" -m pip show selenium >nul 2>nul
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" watercode.py %*
set "result=%errorlevel%"
echo.
pause
exit /b %result%

:fail
echo Python environment setup failed. Install Python 3.10+ and check network access.
pause
exit /b 1
