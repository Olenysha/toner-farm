#!/usr/bin/env sh
set -e

# Инициализация БД (idempotent): создаёт схему, миграции, сид пустых таблиц.
python - <<'PY'
from db import init_db
init_db()
print("[DB] initialized")
PY

# Фоновый SNMP-опрос принтеров — отдельный процесс:
# gunicorn не выполняет __main__ из app.py, поэтому start_snmp_polling() там не сработает.
python snmp_poller.py &

# HTTPS: SSL-логика из app.py под gunicorn не выполняется (__main__ не запускается),
# поэтому TLS поднимаем здесь — если пара сертификат+ключ доступна
# (env или ./certs/*.pem внутри контейнера).
CERT="${CERT_FILE:-/app/certs/cert.pem}"
KEY="${KEY_FILE:-/app/certs/key.pem}"
SSL_ARGS=""
if [ -f "$CERT" ] && [ -f "$KEY" ]; then
    SSL_ARGS="--certfile $CERT --keyfile $KEY"
    echo "[HTTPS] включён: $CERT"
fi

# Редиректор http → https: позволяет открывать сайт просто по имени хоста
# (без https:// в адресной строке). Включается только вместе с TLS.
if [ -n "$SSL_ARGS" ] && [ -n "${HTTP_REDIRECT_PORT:-}" ]; then
    python http_redirect.py "$HTTP_REDIRECT_PORT" "${PORT:-5000}" &
    echo "[redirect] http :$HTTP_REDIRECT_PORT → https (порт ${PORT:-5000})"
fi

# Запуск production WSGI-сервера.
exec gunicorn -b 0.0.0.0:${PORT:-5000} \
    -w 2 --threads 4 \
    --timeout 30 \
    --access-logfile - \
    --error-logfile - \
    $SSL_ARGS \
    app:app
