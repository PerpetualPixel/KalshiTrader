@echo off
REM KalshiTrader auto-login wrapper for Windows
REM Place this file in Desktop\KalshiTrade or add the repo to PATH

setlocal enabledelayedexpansion

REM Get the directory where this batch file is located
set SCRIPT_DIR=%~dp0

REM Run the Python script from the repo directory
cd /d "%SCRIPT_DIR%"
python auto_login.py %*

endlocal
