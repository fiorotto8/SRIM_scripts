@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_srim_no_admin.ps1" %*
