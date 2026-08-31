try {
    # 1. Удаляем автоматические блокирующие правила python.exe из обоих сторов
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
                    }
                } catch {}
            }
        } catch {}
    }

    # 2. Удаляем правила с именем python.exe (если есть)
    try {
        Remove-NetFirewallRule -Name 'python.exe' -PolicyStore PersistentStore -ErrorAction SilentlyContinue
        Remove-NetFirewallRule -Name 'python.exe' -PolicyStore ActiveStore -ErrorAction SilentlyContinue
    } catch {}

    # 3. Переводим активное подключение в частный профиль
    $profile = Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -eq 'Internet' } | Select-Object -First 1
    if ($profile) {
        Set-NetConnectionProfile -InterfaceIndex $profile.InterfaceIndex -NetworkCategory Private
    }

    $log = Join-Path $PSScriptRoot 'fix-fw.log'
    "OK removed=$removed" | Out-File $log -Encoding utf8
} catch {
    "ERROR: $_" | Out-File (Join-Path $PSScriptRoot 'fix-fw.log') -Encoding utf8
}
