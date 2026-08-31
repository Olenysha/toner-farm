@echo off
chcp 866 >nul
net session >nul 2>&1
if errorlevel 1 (
    echo Требуются права администратора. Перезапускаю с повышением...
    powershell -Command "Start-Process 'cmd' -ArgumentList '/k \"\"%~f0\"\"' -Verb RunAs"
    exit /b
)

echo [1/2] Удаляю блокирующие правила python.exe ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix-fw.ps1"

echo [2/2] Добавляю/обновляю разрешающее правило для порта 5000 ...
netsh advfirewall firewall delete rule name="TonerFarm-5000" >nul 2>&1
netsh advfirewall firewall add rule name="TonerFarm-5000" dir=in action=allow protocol=TCP localport=5000 profile=public,private,domain localip=any remoteip=any

echo.
echo Готово.
echo.
echo Нажмите любую клавишу для выхода...
pause >nul
