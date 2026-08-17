# Тонер-фарм — all-in-one образ (Flask + SQLite + SNMP-мониторинг)
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    TZ=Europe/Moscow

# tzdata — чтобы метки времени опросов/операций были в локальном времени,
# а не в UTC (иначе на карте принтеры выглядят «не опрошенными 3 часа»)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# корпоративный файрвол подменяет TLS-сертификаты. Его CA добавляем в trust
# store (для requests и пр.), но pip дополнительно требует --trusted-host:
# Python 3.13 проверяет цепочку в strict-режиме, а у файрвола Basic
# Constraints промежуточного CA не помечены critical — проверка не пройдёт
COPY corp-ca-intermediate.crt corp-ca-root.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

# зависимости отдельным слоем — кэшируются при изменении кода
COPY requirements.txt .
RUN pip install --no-cache-dir \
    --trusted-host pypi.org --trusted-host files.pythonhosted.org \
    -r requirements.txt

COPY . .

# данные (database.db, alerts.db, backups/) живут в volume и переживают
# пересоздание контейнера
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 5000
CMD ["python", "app.py"]
