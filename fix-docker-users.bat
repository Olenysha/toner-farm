@echo off
rem Добавляет текущего пользователя в группу docker-users.
rem Без этого Docker Desktop падает с "Access denied to Docker".
rem После выполнения — выйти из системы и зайти снова (или перезагрузить).
net session >nul 2>&1
if errorlevel 1 (
    echo Требуются права администратора — перезапускаю с повышением...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
net localgroup docker-users "%USERNAME%" /add
echo.
echo Готово: %USERNAME% добавлен в docker-users.
echo ВАЖНО: выйдите из Windows и зайдите снова (или перезагрузитесь),
echo затем запустите Docker Desktop.
pause
