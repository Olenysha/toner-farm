try {
    $rules = Get-NetFirewallRule -PolicyStore ActiveStore | Where-Object { $_.Action -eq 'Block' -and $_.Direction -eq 'Inbound' }
    $removed = 0
    foreach ($r in $rules) {
        $app = $r | Get-NetFirewallApplicationFilter
        if ($app.Program -like '*python*') {
            Remove-NetFirewallRule -Name $r.Name -PolicyStore ActiveStore
            $removed++
        }
    }
    "OK removed=$removed" | Out-File 'E:\Geropharm\BD sklada\toner_farm\fix-fw.log' -Encoding utf8
} catch {
    "ERROR: $_" | Out-File 'E:\Geropharm\BD sklada\toner_farm\fix-fw.log' -Encoding utf8
}
