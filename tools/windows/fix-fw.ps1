try {
    Write-Host '[fix-fw] Удаляю блокирующие правила python.exe ...' -ForegroundColor Cyan

    $stores = @('ActiveStore', 'PersistentStore')
    $removed = 0
    foreach ($store in $stores) {
        try {
            $rules = Get-NetFirewallRule -PolicyStore $store -ErrorAction SilentlyContinue |
                Where-Object { $_.Action -eq 'Block' -and $_.Direction -eq 'Inbound' }
            foreach ($r in $rules) {
                try {
                    $app = $r | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue
                    if ($app.Program -like '*python*') {
                        Remove-NetFirewallRule -Name $r.Name -PolicyStore $store -ErrorAction SilentlyContinue
                        $removed++
                        Write-Host ('  удалено: ' + $r.DisplayName + ' (' + $app.Program + ')') -ForegroundColor Green
                    }
                } catch {}
            }
        } catch {}
    }

    try {
        Remove-NetFirewallRule -Name 'python.exe' -PolicyStore PersistentStore -ErrorAction SilentlyContinue
        Remove-NetFirewallRule -Name 'python.exe' -PolicyStore ActiveStore -ErrorAction SilentlyContinue
        Write-Host '  удалены правила с именем python.exe' -ForegroundColor Green
    } catch {}

    Write-Host '[fix-fw] Перевожу активное подключение в частный профиль ...' -ForegroundColor Cyan
    $profile = Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -eq 'Internet' } | Select-Object -First 1
    if ($profile) {
        Set-NetConnectionProfile -InterfaceIndex $profile.InterfaceIndex -NetworkCategory Private
        Write-Host ('  профиль ' + $profile.Name + ' -> Private') -ForegroundColor Green
    } else {
        Write-Host '  активное подключение не найдено' -ForegroundColor Yellow
    }

    $log = Join-Path $PSScriptRoot 'fix-fw.log'
    "OK removed=$removed" | Out-File $log -Encoding utf8
    Write-Host ('[fix-fw] Готово. Удалено правил: ' + $removed) -ForegroundColor Green
} catch {
    $msg = 'ERROR: ' + $_
    $msg | Out-File (Join-Path $PSScriptRoot 'fix-fw.log') -Encoding utf8
    Write-Host $msg -ForegroundColor Red
}
