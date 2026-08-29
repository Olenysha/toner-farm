@echo off
rem Устраняет таймаут веб-морды Тонер-Фарм с других устройств в локалке.
rem Причина: Windows помечает Wi-Fi как "Общественная сеть" и блокирует
rem входящие подключения к python.exe. Блок-правило сильнее разрешающего
rem правила для порта 5000, поэтому телефон получает таймаут.
net session >nul 2>&1
if errorlevel 1 (
    echo Требуются права администратора — перезапускаю с повышением...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo [1/3] Удаляю блокирующие правила для python.exe ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "^
    $r = Get-NetFirewallRule -PolicyStore ActiveStore | Where-Object { $_.Action -eq 'Block' -and $_.Direction -eq 'Inbound' };^
    $n = 0;^
    foreach ($x in $r) {^
        $app = $x | Get-NetFirewallApplicationFilter;^
        if ($app.Program -like '*python*') {^
            Remove-NetFirewallRule -Name $x.Name -PolicyStore ActiveStore;^
            $n++^
        }^
    };^
    Write-Host ('Удалено блокирующих правил python.exe: ' + $n)"

echo [2/3] Перевожу активное подключение в частный профиль ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "^
    $p = Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -eq 'Internet' } | Select-Object -First 1;^
    if ($p) {^
        Set-NetConnectionProfile -InterfaceIndex $p.InterfaceIndex -NetworkCategory Private;^
        Write-Host ('Профиль сети ' + $p.Name + ' изменен на Private')^
    } else {^
        Write-Host 'Активное подключение не найдено' }"

echo [3/3] Добавляю/обновляю разрешающее правило для порта 5000 ...
netsh advfirewall firewall delete rule name="TonerFarm-5000-Public" >nul 2>&1
netsh advfirewall firewall add rule name="TonerFarm-5000-Public" dir=in action=allow protocol=TCP localport=5000 profile=public,private,domain localip=any remoteip=any

echo.
echo Готово. Проверь с телефона: https://192.168.1.60:5000
echo Если все еще не открывается — выключи мобильный интернет на телефоне и убедись, что он в той же Wi-Fi сети.
pause
