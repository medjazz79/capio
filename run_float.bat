@echo off
setlocal
wscript.exe //B //NoLogo "%~dp0setup\run_float_hidden.vbs"
exit /b %errorlevel%
