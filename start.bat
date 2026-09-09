@echo off
rem Double-click launcher for KalshiTrader. Runs start.ps1 with the execution policy bypassed
rem for this process only. Extra arguments are passed through (e.g. start.bat -Scan).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
