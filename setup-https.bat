@echo off
rem Читаем ps1 явно как UTF-8: тогда скрипт работает независимо от BOM
rem (файл без BOM Windows PowerShell 5.1 читал бы как ANSI и падал на кириллице)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ". ([scriptblock]::Create([IO.File]::ReadAllText('%~dp0setup-https.ps1', [Text.Encoding]::UTF8)))"
pause
