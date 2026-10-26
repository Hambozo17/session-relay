@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-bundle.ps1"
exit /b %ERRORLEVEL%
