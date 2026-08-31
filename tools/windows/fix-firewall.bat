@echo off
rem Устраняет таймаут веб-морды Тонер-Фарм с других устройств в локалке.
rem Причина: Windows помечает Wi-Fi как "Общественная сеть" и блокирует
rem входящие подключения к python.exe. Блок-правило сильнее разрешающего
rem правила для порта 5000, поэтому телефон получает таймаут.
net session >nul 2>&1
if errorlevel 1 (
    echo Требуются права администратора - перезапускаю с повышением...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo [1/4] Удаляю блокирующие правила python.exe по имени через netsh ...
netsh advfirewall firewall delete rule name="python.exe" >nul 2>&1
if %errorlevel% equ 0 (
    echo   Удалены правила с именем "python.exe"
) else (
    echo   Правил с именем "python.exe" не найдено или уже удалены
)

echo [2/4] Удаляю блокирующие правила python.exe по пути программы ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "^
    $stores = @('ActiveStore','PersistentStore');^
    $removed = 0;^
    foreach ($store in $stores) {^
        try {^
            $rules = Get-NetFirewallRule -PolicyStore $store -ErrorAction SilentlyContinue |^
                Where-Object { $_.Action -eq 'Block' -and $_.Direction -eq 'Inbound' };^
            foreach ($x in $rules) {^
                try {^
                    $app = $x | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue;^
                    if ($app.Program -like '*python*') {^
                        Remove-NetFirewallRule -Name $x.Name -PolicyStore $store -ErrorAction SilentlyContinue;^
                        $removed++;^
                    }^
                } catch {}^
            }^
        } catch {}^
    };^
    Write-Host ('  Удалено блокирующих правил python.exe: ' + $removed)"

echo [3/4] Перевожу активное подключение в частный профиль ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "^
    $p = Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -eq 'Internet' } | Select-Object -First 1;^
    if ($p) {^
        Set-NetConnectionProfile -InterfaceIndex $p.InterfaceIndex -NetworkCategory Private;^
        Write-Host ('  Профиль сети ' + $p.Name + ' изменен на Private')^
    } else {^
        Write-Host '  Активное подключение не найдено' }"

echo [4/4] Добавляю/обновляю разрешающее правило для порта 5000 ...
netsh advfirewall firewall delete rule name="TonerFarm-5000" >nul 2>&1
netsh advfirewall firewall add rule name="TonerFarm-5000" dir=in action=allow protocol=TCP localport=5000 profile=public,private,domain localip=any remoteip=any

echo.
echo Готово. Проверь с телефона: https://^<IP_ноутбука^>:5000
echo Чтобы узнать IP, выполни в PowerShell: Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp
echo Если все еще не открывается - выключи мобильный интернет на телефоне и убедись, что он в той же Wi-Fi сети.
pause
