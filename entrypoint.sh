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

# Запуск production WSGI-сервера.
exec gunicorn -b 0.0.0.0:${PORT:-5000} \
    -w 2 --threads 4 \
    --timeout 30 \
    --access-logfile - \
    --error-logfile - \
    app:app
