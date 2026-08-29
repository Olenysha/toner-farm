@echo off
rem Открывает порт 5000 в брандмауэре Windows (вместо полного отключения).
rem Запустить один раз — правило постоянное.
net session >nul 2>&1
if errorlevel 1 (
    echo Требуются права администратора — перезапускаю с повышением...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
netsh advfirewall firewall delete rule name="Toner-Farm 5000" >nul 2>&1
netsh advfirewall firewall add rule name="Toner-Farm 5000" dir=in action=allow protocol=TCP localport=5000
echo.
echo Готово: входящие на TCP-порт 5000 разрешены.
echo Брандмауэр можно оставить включённым.
pause
