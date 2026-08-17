#Requires -Version 5.1
<#
  setup-https.ps1
  Одна кнопка — HTTPS для Тонер-фарм.
  Определяет текущий IP, скачивает mkcert, создаёт сертификат,
  патчит app.py и выводит инструкцию по установке CA на телефон.
#>
$ErrorActionPreference = 'Stop'
# $PSCommandPath заполнен при запуске -File; через scriptblock (bat читает
# файл как UTF-8) он пуст — тогда берём текущую папку (bat уже сделал cd)
$scriptDir = if ($PSCommandPath) { Split-Path -Parent $PSCommandPath } else { (Get-Location).Path }
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
$localIP | Set-Content -Path (Join-Path $scriptDir 'last_ip.txt') -Encoding UTF8 -NoNewline
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
$caPemDst = Join-Path (Join-Path $scriptDir 'static') 'rootCA.pem'
if (Test-Path $caPemSrc) {
    Copy-Item -Path $caPemSrc -Destination $caPemDst -Force
    Write-Host "✅ CA-файл для телефона скопирован в static/rootCA.pem" -ForegroundColor Green
}

# ── 5б. Копия пары для Docker (монтируется в контейнер как ./certs) ────
$certsDir = Join-Path $scriptDir 'certs'
New-Item -ItemType Directory -Force -Path $certsDir | Out-Null
Copy-Item -Path $certPem -Destination (Join-Path $certsDir 'cert.pem') -Force
Copy-Item -Path $keyPem  -Destination (Join-Path $certsDir 'key.pem') -Force
Write-Host "✅ Копия сертификата для Docker: certs/cert.pem + certs/key.pem" -ForegroundColor Green

# ── 6. Патчим app.py ─────────────────────────────────────────────────
$appPy = Join-Path $scriptDir 'app.py'
$appLines = Get-Content -Path $appPy -Encoding UTF8
$startIndex = -1
for ($i = 0; $i -lt $appLines.Count; $i++) {
    if ($appLines[$i] -match "if __name__ == '__main__':") {
        $startIndex = $i
        break
    }
}

$newBlock = @(
    "if __name__ == '__main__':",
    "    import os, socket",
    "",
    "    # IP, для которого создан сертификат (setup-https сохраняет его в last_ip.txt)",
    "    ip_file = os.path.join(BASE_DIR, 'last_ip.txt')",
    "    if os.path.exists(ip_file):",
    "        with open(ip_file, 'r', encoding='utf-8-sig') as f:",
    "            local_ip = f.read().strip()",
    "        if not local_ip:",
    "            local_ip = None",
    "    else:",
    "        local_ip = None",
    "",
    "    # Fallback: авто-определение текущего IP",
    "    if not local_ip:",
    "        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)",
    "        try:",
    "            s.connect(('8.8.8.8', 80))",
    "            local_ip = s.getsockname()[0]",
    "        except Exception:",
    "            local_ip = '127.0.0.1'",
    "        finally:",
    "            s.close()",
    "",
    "    # Сертификат: явные пути из env (Docker монтирует ./certs) или по имени IP",
    "    cert = os.environ.get('CERT_FILE') or os.path.join(BASE_DIR, f'{local_ip}+2.pem')",
    "    key = os.environ.get('KEY_FILE') or os.path.join(BASE_DIR, f'{local_ip}+2-key.pem')",
    "    if os.path.exists(cert) and os.path.exists(key):",
    "        ssl_ctx = (cert, key)",
    "        app.config['HTTPS_ENABLED'] = True",
    "        print(f'[HTTPS] https://{local_ip}:5000')",
    "    else:",
    "        ssl_ctx = None",
    "        app.config['HTTPS_ENABLED'] = False",
    "        print(f'[HTTP]  http://{local_ip}:5000 (сканер не заработает без HTTPS — запусти setup-https.bat)')",
    "",
    "    # Фоновый SNMP-опрос принтеров",
    "    start_snmp_polling()",
    "",
    "    app.run(host='0.0.0.0', port=5000, ssl_context=ssl_ctx)"
)

if ($startIndex -ge 0) {
    if ($startIndex -eq 0) {
        $newLines = $newBlock
    } else {
        $newLines = $appLines[0..($startIndex - 1)] + $newBlock
    }
    Set-Content -Path $appPy -Value $newLines -Encoding UTF8
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
