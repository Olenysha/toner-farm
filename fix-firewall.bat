@echo off
rem Удаляет автоматические БЛОКИРУЮЩИЕ правила брандмауэра для python.exe
rem (Windows создаёт их при первом запуске Python, если в запросе сетевого
rem доступа нажать "Отмена" или не отметить "Публичные сети").
rem Блок-правило для программы сильнее разрешающего правила порта 5000,
rem поэтому из локальной сети сервер был недоступен (таймаут).
net session >nul 2>&1
if errorlevel 1 (
    echo Требуются права администратора — перезапускаю с повышением...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$r = Get-NetFirewallRule -PolicyStore ActiveStore | Where-Object { $_.Action -eq 'Block' -and $_.Direction -eq 'Inbound' }; $n = 0; foreach ($x in $r) { if (($x | Get-NetFirewallApplicationFilter).Program -like '*python*') { Remove-NetFirewallRule -Name $x.Name -PolicyStore ActiveStore; $n++ } }; Write-Host ('Удалено блокирующих правил python.exe: ' + $n)"
echo.
echo Готово. Проверь с телефона: https://192.168.1.60:5000
pause
