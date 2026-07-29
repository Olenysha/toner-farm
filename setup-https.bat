@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "setup-https.ps1"
pause
