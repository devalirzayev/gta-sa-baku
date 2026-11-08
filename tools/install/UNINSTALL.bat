@echo off
rem Baku for GTA San Andreas - uninstaller (restores the original game)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1" -Uninstall %*
pause
