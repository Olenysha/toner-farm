"""Отдельный процесс для фонового SNMP-опроса принтеров в Docker.

Gunicorn не выполняет блок `if __name__ == '__main__'` в app.py,
поэтому опрос запускается отдельным процессом из entrypoint.sh.
"""
import time

from db import init_db
from snmp_monitor import start_snmp_polling

init_db()
start_snmp_polling()
print('[SNMP] poller started', flush=True)

while True:
    time.sleep(3600)
