@echo off
title Install Dependencies — Battery Paper Agent
echo.
echo  Battery Paper Report Agent — Dependency Installer
echo  ==================================================
echo.
echo  Installing required Python packages...
echo.

pip install feedparser PyYAML jinja2 python-Levenshtein certifi

echo.
if %ERRORLEVEL% NEQ 0 (
    echo  [ERROR] Install failed. Make sure Python is installed and pip is on your PATH.
    echo  Download Python from: https://www.python.org/downloads/
) else (
    echo  [OK] All dependencies installed successfully!
    echo  You can now run run_daily_paper.bat
)
echo.
pause
