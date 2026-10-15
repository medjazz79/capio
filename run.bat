@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1" -Mode reader
if errorlevel 1 pause
exit /b %errorlevel%
