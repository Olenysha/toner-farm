"""Константы Тонер-Фарм, вынесенные из кода в отдельный файл.

Все настройки, которые могут меняться между площадками,
также читаются из переменных окружения (см. auth.py, docker-compose.yml).
"""
import os

# --- Склад / статусы ----------------------------------------------------------
# Сколько дней тонер считается «стареющим» (жёлтый статус на карте)
AGING_DAYS = 60

# Соответствие цвета картриджа колонке-слоту принтера
SLOT_COLUMN = {
    'Black': 'toner_bk_id',
    'Cyan': 'toner_c_id',
    'Magenta': 'toner_m_id',
    'Yellow': 'toner_y_id',
}

# Сколько подряд неудачных SNMP-опросов переводит принтер в серый статус
SNMP_FAIL_GREY = 5

# Детекция замены тонера «мимо системы»: скачок уровня SNMP
# с <= TONER_CHANGE_LOW до >= TONER_CHANGE_HIGH = тонер заменили вручную.
# По такому событию UI спрашивает пользователя и предлагает списать тонер со склада.
TONER_CHANGE_LOW = int(os.environ.get('TONER_CHANGE_LOW', '2'))
TONER_CHANGE_HIGH = int(os.environ.get('TONER_CHANGE_HIGH', '90'))

# --- SNMP ----------------------------------------------------------------------
COMMUNITY = os.environ.get('SNMP_COMMUNITY', 'public')
SNMP_TIMEOUT = 3            # секунд на один SNMP-запрос
SNMP_INTERVAL = int(os.environ.get('SNMP_INTERVAL', '600'))  # секунд между фоновыми опросами
WALK_MAX_STEPS = 500        # страховка от бесконечного walk

# --- Агенты SNMP (удалённые сети) --------------------------------------------
AGENT_TOKEN = os.environ.get('AGENT_TOKEN', '')

# --- Карты / загрузки ---------------------------------------------------------
ALLOWED_PLAN_EXT = {'.jpg', '.jpeg', '.png', '.webp'}

# prtAlertSeverityLevel: other(1), critical(3), serious(4), warning(5)
SEVERITY_ICON = {
    '3': '🔴',
    '4': '🟠',
    '5': '🟡',
    '1': 'ℹ️',
}
