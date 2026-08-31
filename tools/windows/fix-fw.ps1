try {
    # 1. Удаляем автоматические блокирующие правила python.exe
    $rules = Get-NetFirewallRule -PolicyStore ActiveStore | Where-Object { $_.Action -eq 'Block' -and $_.Direction -eq 'Inbound' }
    $removed = 0
    foreach ($r in $rules) {
        $app = $r | Get-NetFirewallApplicationFilter
        if ($app.Program -like '*python*') {
            Remove-NetFirewallRule -Name $r.Name -PolicyStore ActiveStore
            $removed++
        }
    }

    # 2. Переводим активное подключение в частный профиль
    $profile = Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -eq 'Internet' } | Select-Object -First 1
    if ($profile) {
        Set-NetConnectionProfile -InterfaceIndex $profile.InterfaceIndex -NetworkCategory Private
    }

    $log = Join-Path $PSScriptRoot 'fix-fw.log'
    "OK removed=$removed" | Out-File $log -Encoding utf8
} catch {
    "ERROR: $_" | Out-File (Join-Path $PSScriptRoot 'fix-fw.log') -Encoding utf8
}
