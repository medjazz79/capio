@echo off
setlocal
start "" /min powershell.exe -NoProfile -WindowStyle Minimized -ExecutionPolicy Bypass -File "%~dp0setup\bootstrap.ps1" -Mode float
exit /b %errorlevel%
