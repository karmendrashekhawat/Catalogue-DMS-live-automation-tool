@echo off
title DealerCenter Media Uploader — Spyne

echo.
echo   DealerCenter Media Uploader - Spyne
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found. Install Python 3.8+ from python.org
    pause
    exit /b 1
)

echo  [1/3] Installing dependencies (first run only)...
python -m pip install -q playwright python-dotenv

echo  [2/3] Installing Playwright browser (first run only)...
python -m playwright install chromium

echo  [3/3] Starting control panel...
echo.
echo  Opening http://localhost:7433
echo  Press Ctrl+C here to stop.
echo.

python "%~dp0server.py"
pause
