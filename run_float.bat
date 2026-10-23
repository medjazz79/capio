@echo off
setlocal
if /i not "%~1"=="--minimized" (
    start "" /min "%ComSpec%" /d /c ""%~f0" --minimized"
    exit /b 0
)
wscript.exe //B //NoLogo "%~dp0setup\run_float_hidden.vbs"
exit /b %errorlevel%
