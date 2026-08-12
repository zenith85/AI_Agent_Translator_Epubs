@echo off
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    python gui.py
    goto :checkresult
)

where py >nul 2>nul
if %errorlevel%==0 (
    py gui.py
    goto :checkresult
)

echo Python was not found on PATH.
echo Install it from https://www.python.org/downloads/ and check "Add python.exe to PATH" during setup.
pause
exit /b 1

:checkresult
if %errorlevel% neq 0 (
    echo.
    echo gui.py exited with an error ^(code %errorlevel%^). See above for details.
    pause
)
