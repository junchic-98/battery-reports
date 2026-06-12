@echo off
title Battery Paper Report Agent
echo.
echo  Battery Paper Report Agent
echo  ===========================
echo.
cd /d "%~dp0"
if exist data\papers.db del /q data\papers.db
python run_daily.py
echo.
if %ERRORLEVEL% NEQ 0 (echo  [ERROR] Something went wrong. See above.) else (echo  Done!)
echo.
pause
