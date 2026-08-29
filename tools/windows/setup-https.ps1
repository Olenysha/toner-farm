#Requires -Version 5.1
<#
  setup-https.ps1
  Выпускает сертификат mkcert для текущего IP и раскладывает его:
  - ./certs/cert.pem + key.pem (для Docker и нативного запуска)
  - static/rootCA.pem (для установки на телефон)

  Больше не патчит app.py: приложение само видит ./certs/*.pem.
#>
$ErrorActionPreference = 'Stop'
# $PSCommandPath заполнен при запуске -File; через scriptblock (bat читает
# файл как UTF-8) он пуст — тогда берём текущую папку (bat уже сделал cd)
$scriptDir = if ($PSCommandPath) { Split-Path -Parent $PSCommandPath } else { (Get-Location).Path }
# Корень проекта: tools/windows -> ..
$projectRoot = Split-Path -Parent $scriptDir
Set-Location $projectRoot

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
$localIP | Set-Content -Path (Join-Path $projectRoot 'last_ip.txt') -Encoding UTF8 -NoNewline
if (-not $localIP) {
    Write-Host "❌ Не удалось определить локальный IP. Убедись, что Wi-Fi подключен." -ForegroundColor Red
    exit 1
}
Write-Host "✅ Текущий IP ноутбука: $localIP" -ForegroundColor Green

# ── 2. Скачиваем mkcert (если нет) ──────────────────────────────────
$mkcertUrl = 'https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-windows-amd64.exe'
$mkcertExe = Join-Path $projectRoot 'mkcert.exe'

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
$certPem = Join-Path $projectRoot "$certName.pem"
$keyPem  = Join-Path $projectRoot "$certName-key.pem"

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
$caPemDst = Join-Path (Join-Path $projectRoot 'static') 'rootCA.pem'
if (Test-Path $caPemSrc) {
    Copy-Item -Path $caPemSrc -Destination $caPemDst -Force
    Write-Host "✅ CA-файл для телефона скопирован в static/rootCA.pem" -ForegroundColor Green
}

# ── 6. Копия пары для Docker (монтируется в контейнер как ./certs) ────
$certsDir = Join-Path $projectRoot 'certs'
New-Item -ItemType Directory -Force -Path $certsDir | Out-Null
Copy-Item -Path $certPem -Destination (Join-Path $certsDir 'cert.pem') -Force
Copy-Item -Path $keyPem  -Destination (Join-Path $certsDir 'key.pem') -Force
Write-Host "✅ Копия сертификата для Docker: certs/cert.pem + certs/key.pem" -ForegroundColor Green

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
