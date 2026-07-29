#Requires -Version 5.1
<#
  setup-https.ps1
  Одна кнопка — HTTPS для Тонер-фарм.
  Определяет текущий IP, скачивает mkcert, создаёт сертификат,
  патчит app.py и выводит инструкцию по установке CA на телефон.
#>
$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $scriptDir

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "   Тонер-фарм — настройка HTTPS" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. Определяем текущий локальный IP ──────────────────────────────
function Get-LocalIP {
    # Берём интерфейс с дефолтным маршрутом
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
             Where-Object { $_.NextHop -and $_.NextHop -ne '0.0.0.0' } |
             Select-Object -First 1
    if ($route) {
        $ip = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
              Where-Object { $_.IPAddress -notlike '169.*' } |
              Select-Object -ExpandProperty IPAddress -First 1
        if ($ip) { return $ip }
    }
    # fallback: первый приватный IP
    $ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
          Where-Object {
              $_.IPAddress -notlike '127.*' -and
              $_.IPAddress -notlike '169.*' -and
              ($_.IPAddress -like '10.*' -or $_.IPAddress -like '172.1[6-9].*' -or $_.IPAddress -like '172.2[0-9].*' -or $_.IPAddress -like '172.3[0-1].*' -or $_.IPAddress -like '192.168.*')
          } |
          Select-Object -ExpandProperty IPAddress -First 1
    return $ip
}

$localIP = Get-LocalIP
if (-not $localIP) {
    Write-Host "❌ Не удалось определить локальный IP. Убедись, что Wi-Fi подключен." -ForegroundColor Red
    exit 1
}
Write-Host "✅ Текущий IP ноутбука: $localIP" -ForegroundColor Green

# ── 2. Скачиваем mkcert (если нет) ──────────────────────────────────
$mkcertUrl = 'https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-windows-amd64.exe'
$mkcertExe = Join-Path $scriptDir 'mkcert.exe'

if (-not (Test-Path $mkcertExe)) {
    Write-Host "⬇️  Скачиваю mkcert.exe..." -ForegroundColor Yellow
    try {
        Invoke-WebRequest -Uri $mkcertUrl -OutFile $mkcertExe -UseBasicParsing
        Write-Host "✅ mkcert.exe скачан" -ForegroundColor Green
    } catch {
        Write-Host "❌ Не удалось скачать mkcert. Проверь интернет." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "✅ mkcert.exe уже есть" -ForegroundColor Green
}

# ── 3. Устанавливаем локальный CA ────────────────────────────────────
Write-Host "🔐 Устанавливаю локальный удостоверяющий центр (CA)..." -ForegroundColor Yellow
& $mkcertExe -install
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Ошибка при установке CA. Запусти скрипт от имени администратора." -ForegroundColor Red
    exit 1
}
Write-Host "✅ CA установлен в систему" -ForegroundColor Green

# ── 4. Создаём сертификат для текущего IP ───────────────────────────
$certName = "$localIP+2"
$certPem = Join-Path $scriptDir "$certName.pem"
$keyPem  = Join-Path $scriptDir "$certName-key.pem"

Write-Host "📜 Создаю сертификат для $localIP, localhost, 127.0.0.1..." -ForegroundColor Yellow
& $mkcertExe $localIP localhost 127.0.0.1
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Ошибка при создании сертификата." -ForegroundColor Red
    exit 1
}
Write-Host "✅ Сертификат создан:" -ForegroundColor Green
Write-Host "   $certPem" -ForegroundColor DarkGray
Write-Host "   $keyPem" -ForegroundColor DarkGray

# ── 5. Копируем CA для телефона ──────────────────────────────────────
$caRoot = & $mkcertExe -CAROOT
$caPemSrc = Join-Path $caRoot 'rootCA.pem'
$caPemDst = Join-Path $scriptDir 'static' 'rootCA.pem'
if (Test-Path $caPemSrc) {
    Copy-Item -Path $caPemSrc -Destination $caPemDst -Force
    Write-Host "✅ CA-файл для телефона скопирован в static/rootCA.pem" -ForegroundColor Green
}

# ── 6. Патчим app.py ─────────────────────────────────────────────────
$appPy = Join-Path $scriptDir 'app.py'
$appContent = Get-Content -Path $appPy -Raw -Encoding UTF8

# Удаляем старый блок запуска (если есть наш патч)
$oldPattern = "if __name__ == '__main__':\s*\n(?:    .*\n)*"
$newBlock = @"
if __name__ == '__main__':
    import socket
    # Авто-определение локального IP для HTTPS
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'
    finally:
        s.close()

    # Ищем сертификат для текущего IP
    cert_file = os.path.join(BASE_DIR, f'{local_ip}+2.pem')
    key_file  = os.path.join(BASE_DIR, f'{local_ip}+2-key.pem')
    if os.path.exists(cert_file) and os.path.exists(key_file):
        ssl_ctx = (cert_file, key_file)
        print(f'🔒 HTTPS: https://{local_ip}:5000')
    else:
        ssl_ctx = None
        print(f'⚠️  HTTP:  http://{local_ip}:5000 (сканер не заработает без HTTPS)')
    app.run(host='0.0.0.0', port=5000, ssl_context=ssl_ctx)
"@

if ($appContent -match "if __name__ == '__main__':") {
    $appContent = [regex]::Replace($appContent, $oldPattern, $newBlock)
    Set-Content -Path $appPy -Value $appContent -Encoding UTF8 -NoNewline
    Write-Host "✅ app.py обновлён для HTTPS" -ForegroundColor Green
} else {
    Write-Host "⚠️  Не удалось найти блок запуска в app.py — проверь вручную." -ForegroundColor Yellow
}

# ── 7. Выводим итог ──────────────────────────────────────────────────
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "   ГОТОВО! Инструкция для телефона:" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "1️⃣  Запусти сервер:          python app.py" -ForegroundColor White
Write-Host "2️⃣  На телефоне зайди на:    https://$localIP`:5000/scan" -ForegroundColor White
Write-Host ""
Write-Host "📲 Установка CA на телефон (ОБЯЗАТЕЛЬНО):" -ForegroundColor Yellow
Write-Host ""
Write-Host "   iPhone:" -ForegroundColor Magenta
Write-Host "   • Отправь файл static/rootCA.pem себе (Telegram / почта)" -ForegroundColor Gray
Write-Host "   • Скачай → Настройки → 'Профиль загружен' → Установить" -ForegroundColor Gray
Write-Host "   • Настройки → Основные → Об этом устройстве → Доверие сертификатам" -ForegroundColor Gray
Write-Host "   • Включи переключатель рядом с 'mkcert ...'" -ForegroundColor Gray
Write-Host ""
Write-Host "   Android:" -ForegroundColor Magenta
Write-Host "   • Скачай static/rootCA.pem на телефон" -ForegroundColor Gray
Write-Host "   • Настройки → Безопасность → Установить сертификат → Сертификат ЦС" -ForegroundColor Gray
Write-Host ""
Write-Host "⚠️  ВАЖНО: URL должен начинаться с https:// (не http://)" -ForegroundColor Red
Write-Host ""
