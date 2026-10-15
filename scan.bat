@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\control_cygno_study.ps1" %*
set "scan_exit=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %scan_exit%
