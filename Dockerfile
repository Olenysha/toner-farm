# Тонер-фарм — production-ready образ (Flask + Gunicorn + SQLite + SNMP)
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    TZ=Europe/Moscow \
    PORT=5000

# tzdata — локальное время операций/опросов; ca-certificates + corp CA —
# для pip и requests в корпоративной сети с SSL-инспекцией.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Корпоративный файрвол подменяет TLS-сертификаты. Если файлы есть — добавляем в trust store.
# Вне корпоративной сети они игнорируются.
COPY corp-ca-root.crt corp-ca-intermediate.crt /usr/local/share/ca-certificates/
RUN if [ -s /usr/local/share/ca-certificates/corp-ca-root.crt ]; then update-ca-certificates; fi
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

# Зависимости отдельным слоем — кэшируются при изменении кода.
COPY requirements.txt .
RUN pip install --no-cache-dir \
    --trusted-host pypi.org --trusted-host files.pythonhosted.org \
    -r requirements.txt

COPY . .

# Единый каталог для данных и бэкапов (volume).
RUN mkdir -p /app/data /app/backups /app/static/qr_codes

EXPOSE 5000
CMD ["sh", "entrypoint.sh"]
