@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\bootstrap.ps1" -Mode float
if errorlevel 1 pause
exit /b %errorlevel%
