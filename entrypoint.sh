#!/usr/bin/env sh
set -e

# Инициализация БД (idempotent): создаёт схему, миграции, сид пустых таблиц.
python - <<'PY'
from db import init_db
init_db()
print("[DB] initialized")
PY

# Запуск production WSGI-сервера.
exec gunicorn -b 0.0.0.0:${PORT:-5000} \
    -w 2 --threads 4 \
    --timeout 30 \
    --access-logfile - \
    --error-logfile - \
    app:app
