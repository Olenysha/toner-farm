# Тонер-фарм — all-in-one образ (Flask + SQLite + SNMP-мониторинг)
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data

WORKDIR /app

# зависимости отдельным слоем — кэшируются при изменении кода
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# данные (database.db, alerts.db, backups/) живут в volume и переживают
# пересоздание контейнера
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 5000
CMD ["python", "app.py"]
