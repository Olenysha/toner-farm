# -*- coding: utf-8 -*-
"""
Тонер-фарм — учёт тонеров/картриджей для IT-отдела.
Локальный Flask-сервер для внутренней сети (пилот без авторизации).
"""
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, date, timedelta
from io import BytesIO

import qrcode
from flask import (Flask, g, jsonify, render_template, request, send_file,
                   send_from_directory, url_for)
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from pysnmp.hlapi import (SnmpEngine, CommunityData, UdpTransportTarget,
                          ContextData, ObjectType, ObjectIdentity, getCmd, nextCmd)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, 'database.db')
ALERT_DATABASE = os.path.join(BASE_DIR, 'alerts.db')  # справочник SNMP-алертов
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
QR_DIR = os.path.join(STATIC_DIR, 'qr_codes')
FLOOR_PLAN_PATH = os.path.join(STATIC_DIR, 'floor_plan.png')

# Сколько дней тонер считается «стареющим» (жёлтый статус на карте)
AGING_DAYS = 60

# v2: авторизация / AD-интеграция — сейчас пилот без логина, операции пишутся без подписи пользователя

app = Flask(__name__)


# ---------------------------------------------------------------- БД helpers

def get_db():
    """Соединение с SQLite через flask.g."""
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS barcode_map (
    ean_13 TEXT PRIMARY KEY,
    model_name TEXT,
    color TEXT,
    compatible_printers TEXT,          -- JSON-массив строк моделей принтеров (printers.model)
    page_yield INTEGER
);
CREATE TABLE IF NOT EXISTS toners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ean_13 TEXT,
    status TEXT DEFAULT 'stock',       -- stock / installed / depleted
    current_printer_id INTEGER,
    installed_at TIMESTAMP,
    quantity INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS printers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    model TEXT,
    type TEXT,                          -- mono / color
    slots_count INTEGER,
    x REAL,
    y REAL,
    floor_id INTEGER,                   -- этаж (floor_plans.id), на котором стоит принтер
    ip_address TEXT,                    -- IP для SNMP-опроса (уровень тонера, snmp_readings)
    toner_bk_id INTEGER,
    toner_c_id INTEGER,
    toner_m_id INTEGER,
    toner_y_id INTEGER
);
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    toner_id INTEGER,
    printer_id INTEGER,
    type TEXT,                          -- install / return / auto_depleted / stock_add / depleted
    old_toner_id INTEGER,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_name TEXT
);
CREATE TABLE IF NOT EXISTS floor_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    image_path TEXT
);
CREATE TABLE IF NOT EXISTS snmp_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    black_level INTEGER,
    cyan_level INTEGER,
    magenta_level INTEGER,
    yellow_level INTEGER,
    page_counter INTEGER,
    status_text TEXT,                    -- Running/Warning/Testing/Down
    alerts TEXT,                         -- JSON-массив строк ["503: Требуется внимание", ...]
    raw_data TEXT                        -- JSON для отладки (sys_name/sys_descr)
);
"""

# Соответствие цвета картриджа колонке-слоту принтера
SLOT_COLUMN = {'Black': 'toner_bk_id', 'Cyan': 'toner_c_id',
               'Magenta': 'toner_m_id', 'Yellow': 'toner_y_id'}


def init_db():
    """Создание схемы, миграции и сид демо-данных, если таблицы пустые."""
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    migrate_db(db)
    ensure_alerts_db()
    cur = db.execute('SELECT COUNT(*) FROM printers')
    if cur.fetchone()[0] == 0:
        seed_db(db)
    db.commit()
    db.close()


def migrate_db(db):
    """Миграции существующих БД: два этажа (5 и 9) и привязка принтеров к этажу."""
    cols = [r[1] for r in db.execute('PRAGMA table_info(printers)')]
    if 'floor_id' not in cols:
        db.execute('ALTER TABLE printers ADD COLUMN floor_id INTEGER')
    plans = db.execute('SELECT * FROM floor_plans ORDER BY id').fetchall()
    if '5 этаж' not in {p['name'] for p in plans}:
        if plans:
            # последний (активный) загруженный план считаем планом 5 этажа,
            # строки старых загрузок убираем (сами файлы остаются в static/)
            keep = plans[-1]['id']
            db.execute("UPDATE floor_plans SET name='5 этаж' WHERE id=?", (keep,))
            db.execute('DELETE FROM floor_plans WHERE id != ?', (keep,))
        else:
            db.execute("INSERT INTO floor_plans (name, image_path) VALUES ('5 этаж','static/floor_plan.png')")
    if not db.execute("SELECT 1 FROM floor_plans WHERE name='9 этаж'").fetchone():
        db.execute("INSERT INTO floor_plans (name, image_path) VALUES ('9 этаж','static/floor_plan.png')")
    first_id = db.execute('SELECT MIN(id) FROM floor_plans').fetchone()[0]
    db.execute('UPDATE printers SET floor_id=? WHERE floor_id IS NULL', (first_id,))
    # проставляем IP принтерам, которых однозначно находим в сети Герофарм
    for name, ip in SNMP_IP_MAP.items():
        db.execute('UPDATE printers SET ip_address=? WHERE name=? AND ip_address IS NULL', (ip, name))


# IP-адреса принтеров Герофарм (для SNMP-опроса). Только однозначные
# соответствия имя → IP; неоднозначные (2× P3155dn, 2× M477, 2× 3252ci)
# заполняются вручную в админке.
SNMP_IP_MAP = {
    'Xerox WorkCentre 7220': '10.7.150.241',
    'Kyocera TASKAlfa 3253ci': '10.7.150.233',
    'HP 479 Яна': '10.7.150.239',
    'этикетки': '10.7.150.91',
    'Кадры': '10.7.150.235',
    'HP M277dw Медпреды': '10.7.200.87',
    'ЗП': '10.7.150.244',
    'Ресешпн': '10.7.150.243',
    'Безопас': '10.7.150.246',
}


def seed_db(db):
    """Демо-данные для пилота: реальные принтеры Герофарм."""
    floor_id = db.execute('SELECT MIN(id) FROM floor_plans').fetchone()[0]
    db.executemany(
        'INSERT INTO printers (name, model, type, slots_count, x, y, floor_id, ip_address) VALUES (?,?,?,?,?,?,?,?)',
        [
            ('HP M477fdn (Коворкинг)', 'HP Color LaserJet MFP M477fdn', 'color', 4, 20.0, 30.0, floor_id, '10.7.150.201'),
            ('HP M277dw (Медпредставители)', 'HP Color LaserJet MFP M277dw', 'color', 4, 55.0, 60.0, floor_id, '10.7.200.87'),
            ('HP M477fdn (Кузьмин)', 'HP Color LaserJet MFP M477fdn', 'color', 4, 80.0, 25.0, floor_id, '10.7.150.242'),
            ('HP M479fdn (Секретарь ГД)', 'HP Color LaserJet Pro MFP M479fdn', 'color', 4, 40.0, 50.0, floor_id, '10.7.150.239'),
            ('HP M251n (Buh этикетки)', 'HP LaserJet 200 color M251n', 'color', 4, 15.0, 70.0, floor_id, '10.7.150.91'),
            ('Kyocera MA4000cix (Кадры)', 'Kyocera ECOSYS MA4000cix', 'color', 4, 60.0, 20.0, floor_id, '10.7.150.235'),
            ('Kyocera MA4000cix (ЗП)', 'Kyocera ECOSYS MA4000cix', 'color', 4, 65.0, 25.0, floor_id, '10.7.150.244'),
            ('Kyocera MA4000cix (Ресепшен)', 'Kyocera ECOSYS MA4000cix', 'color', 4, 70.0, 30.0, floor_id, '10.7.150.243'),
            ('Kyocera MA4000cix (Экон.безоп.)', 'Kyocera ECOSYS MA4000cix', 'color', 4, 75.0, 35.0, floor_id, '10.7.150.246'),
            ('Kyocera P3155dn BUH', 'Kyocera ECOSYS P3155dn', 'mono', 1, 50.0, 40.0, floor_id, '10.7.150.237'),
            ('Kyocera P3155dn SALE', 'Kyocera ECOSYS P3155dn', 'mono', 1, 55.0, 45.0, floor_id, '10.7.150.236'),
            ('Kyocera TASKalfa 3252ci (HR/LOG)', 'Kyocera TASKalfa 3252ci', 'color', 4, 30.0, 15.0, floor_id, '10.7.150.231'),
            ('Kyocera TASKalfa 3252ci (LAW/SALE)', 'Kyocera TASKalfa 3252ci', 'color', 4, 35.0, 20.0, floor_id, '10.7.150.232'),
            ('Kyocera TASKalfa 3253ci (FIN)', 'Kyocera TASKalfa 3253ci', 'color', 4, 45.0, 25.0, floor_id, '10.7.150.233'),
            ('Xerox WC 7220', 'Xerox WorkCentre 7220', 'color', 4, 85.0, 80.0, floor_id, '10.7.150.241'),
        ])
    db.executemany(
        'INSERT INTO barcode_map (ean_13, model_name, color, compatible_printers, page_yield) VALUES (?,?,?,?,?)',
        [
            ('0886111244457', 'HP CF410A (410A)', 'Black',
             json.dumps(['HP Color LaserJet M452']), 2300),
            ('0886111244464', 'HP CF411A (410A)', 'Cyan',
             json.dumps(['HP Color LaserJet M452']), 2300),
        ])
    db.executemany(
        "INSERT INTO toners (ean_13, status) VALUES (?, 'stock')",
        [('0886111244457',), ('0886111244457',), ('0886111244464',)])


# ------------------------------------------------- План этажа (заглушка PIL)

def ensure_floor_plan():
    """Рисуем простой план-заглушку, если файла нет."""
    if os.path.exists(FLOOR_PLAN_PATH):
        return
    w, h = 1200, 800
    img = Image.new('RGB', (w, h), '#f4f1ea')
    d = ImageDraw.Draw(img)
    # сетка
    for x in range(0, w, 50):
        d.line([(x, 0), (x, h)], fill='#e3ded2')
    for y in range(0, h, 50):
        d.line([(0, y), (w, y)], fill='#e3ded2')
    # комнаты
    rooms = [
        ((60, 60, 420, 380), 'Бухгалтерия'),
        ((60, 430, 420, 740), 'Коридор'),
        ((470, 60, 780, 380), 'HR'),
        ((470, 430, 780, 740), 'Кухня'),
        ((830, 60, 1140, 380), 'Отдел продаж'),
        ((830, 430, 1140, 740), 'Серверная'),
    ]
    for (x1, y1, x2, y2), label in rooms:
        d.rectangle([x1, y1, x2, y2], outline='#8a8578', width=4)
        d.text(((x1 + x2) // 2 - 40, (y1 + y2) // 2 - 8), label, fill='#6b675d')
    d.text((30, 10), 'Тонер-фарм — план этажа (заглушка, замените в админке)', fill='#4a463e')
    img.save(FLOOR_PLAN_PATH)


# ------------------------------------------------------------------ Бэкапы

def maybe_backup():
    """Раз в день копируем database.db в backups/, храним последние 14."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = date.today().strftime('%Y%m%d')
    name = f'database_{stamp}.db'
    dst = os.path.join(BACKUP_DIR, name)
    if os.path.exists(DATABASE) and not os.path.exists(dst):
        shutil.copy2(DATABASE, dst)
    # ротация: оставляем 14 последних
    files = sorted(f for f in os.listdir(BACKUP_DIR)
                   if f.startswith('database_') and f.endswith('.db'))
    for old in files[:-14]:
        os.remove(os.path.join(BACKUP_DIR, old))


@app.before_request
def before_request():
    maybe_backup()


# ------------------------------------------------------- SNMP-мониторинг

SNMP_INTERVAL = 600  # секунд между фоновыми опросами принтеров

# ------------------------------------------------- Справочник SNMP-алертов

# Коды prtAlertCode по RFC 3805 / IANA-PRINTER-MIB (vendor='*') + поправки
# вендоров. (vendor, code, name, title_ru, hint_ru)
ALERT_CODES_SEED = [
    # --- общие коды (1-38) ---
    ('*', 1, 'other', 'Прочее событие', None),
    ('*', 2, 'unknown', 'Неизвестное событие', None),
    ('*', 3, 'coverOpen', 'Крышка открыта', 'Закрыть крышку'),
    ('*', 4, 'coverClosed', 'Крышка закрыта', None),
    ('*', 5, 'interlockOpen', 'Блокировка открыта', None),
    ('*', 6, 'interlockClosed', 'Блокировка закрыта', None),
    ('*', 7, 'configurationChange', 'Изменение конфигурации', None),
    ('*', 8, 'jam', 'Замятие бумаги', 'Устранить замятие'),
    ('*', 9, 'subunitMissing', 'Узел снят', 'Установить узел на место'),
    ('*', 10, 'subunitLifeAlmostOver', 'Ресурс узла на исходе', None),
    ('*', 11, 'subunitLifeOver', 'Ресурс узла исчерпан', 'Заменить узел'),
    ('*', 12, 'subunitAlmostEmpty', 'Узел почти пуст', None),
    ('*', 13, 'subunitEmpty', 'Узел пуст', None),
    ('*', 14, 'subunitAlmostFull', 'Узел почти полон', None),
    ('*', 15, 'subunitFull', 'Узел полон', None),
    ('*', 16, 'subunitNearLimit', 'Узел у предела', None),
    ('*', 17, 'subunitAtLimit', 'Узел на пределе', None),
    ('*', 18, 'subunitOpened', 'Узел открыт', None),
    ('*', 19, 'subunitClosed', 'Узел закрыт', None),
    ('*', 20, 'subunitTurnedOn', 'Узел включён', None),
    ('*', 21, 'subunitTurnedOff', 'Узел выключен', None),
    ('*', 22, 'subunitOffline', 'Узел офлайн', None),
    ('*', 23, 'subunitPowerSaver', 'Спящий режим', None),
    ('*', 24, 'subunitWarmingUp', 'Прогрев', None),
    ('*', 25, 'subunitAdded', 'Узел добавлен', None),
    ('*', 26, 'subunitRemoved', 'Узел извлечён', None),
    ('*', 27, 'subunitResourceAdded', 'Ресурс добавлен', None),
    ('*', 28, 'subunitResourceRemoved', 'Ресурс извлечён', None),
    ('*', 29, 'subunitRecoverableFailure', 'Устранимый сбой узла', None),
    ('*', 30, 'subunitUnrecoverableFailure', 'Неустранимый сбой узла', 'Требуется вмешательство'),
    ('*', 31, 'subunitRecoverableStorageError', 'Устранимая ошибка памяти', None),
    ('*', 32, 'subunitUnrecoverableStorageError', 'Неустранимая ошибка памяти', None),
    ('*', 33, 'subunitMotorFailure', 'Отказ двигателя', None),
    ('*', 34, 'subunitMemoryExhausted', 'Память исчерпана', None),
    ('*', 35, 'subunitUnderTemperature', 'Недогрев', None),
    ('*', 36, 'subunitOverTemperature', 'Перегрев', None),
    ('*', 37, 'subunitTimingFailure', 'Сбой синхронизации', None),
    ('*', 38, 'subunitThermistorFailure', 'Отказ термистора', None),
    # --- общая группа принтера (501-507) ---
    ('*', 501, 'doorOpen', 'Дверца открыта', 'Закрыть дверцу'),
    ('*', 502, 'doorClosed', 'Дверца закрыта', None),
    ('*', 503, 'powerUp', 'Включение питания', None),
    ('*', 504, 'powerDown', 'Выключение питания', None),
    ('*', 505, 'printerNMSReset', 'Сброс по сети (NMS)', None),
    ('*', 506, 'printerManualReset', 'Ручной сброс', None),
    ('*', 507, 'printerReadyToPrint', 'Готов к печати', None),
    # --- лотки/подача (801-820) ---
    ('*', 801, 'inputMediaTrayMissing', 'Лоток отсутствует', 'Установить лоток'),
    ('*', 802, 'inputMediaSizeChange', 'Изменён размер бумаги', None),
    ('*', 803, 'inputMediaWeightChange', 'Изменена плотность бумаги', None),
    ('*', 804, 'inputMediaTypeChange', 'Изменён тип бумаги', None),
    ('*', 805, 'inputMediaColorChange', 'Изменён цвет бумаги', None),
    ('*', 806, 'inputMediaFormPartsChange', 'Изменён состав формы', None),
    ('*', 807, 'inputMediaSupplyLow', 'Бумага заканчивается', 'Добавить бумагу'),
    ('*', 808, 'inputMediaSupplyEmpty', 'Лоток пуст (нет бумаги)', 'Добавить бумагу'),
    ('*', 809, 'inputMediaChangeRequest', 'Требуется смена бумаги', None),
    ('*', 810, 'inputManualInputRequest', 'Требуется ручная подача', None),
    ('*', 811, 'inputTrayPositionFailure', 'Ошибка позиционирования лотка', None),
    ('*', 812, 'inputTrayElevationFailure', 'Ошибка подъёма лотка', None),
    ('*', 813, 'inputCannotFeedSizeSelected', 'Невозможно подать выбранный размер', None),
    ('*', 814, 'inputMediaTrayFeedError', 'Ошибка подачи из лотка', None),
    ('*', 815, 'inputMediaTrayJam', 'Замятие в лотке', 'Устранить замятие'),
    ('*', 816, 'inputMediaTrayFailure', 'Неисправность лотка', None),
    ('*', 817, 'inputMediaTrayPickRollerLifeWarn', 'Ролик подачи: ресурс на исходе', None),
    ('*', 818, 'inputMediaTrayPickRollerLifeOver', 'Ролик подачи: ресурс исчерпан', 'Заменить ролик'),
    ('*', 819, 'inputMediaTrayPickRollerFailure', 'Отказ ролика подачи', None),
    ('*', 820, 'inputMediaTrayPickRollerMissing', 'Ролик подачи отсутствует', None),
    # --- выход (901-907) ---
    ('*', 901, 'outputMediaTrayMissing', 'Выходной лоток отсутствует', None),
    ('*', 902, 'outputMediaTrayAlmostFull', 'Выходной лоток почти полон', None),
    ('*', 903, 'outputMediaTrayFull', 'Выходной лоток полон', 'Освободить лоток'),
    ('*', 904, 'outputMailboxSelectFailure', 'Ошибка выбора ящика', None),
    ('*', 905, 'outputMediaTrayFeedError', 'Ошибка подачи на выход', None),
    ('*', 906, 'outputMediaTrayJam', 'Замятие на выходе', 'Устранить замятие'),
    ('*', 907, 'outputMediaTrayFailure', 'Неисправность выходного лотка', None),
    # --- маркер/фьюзер (1001-1030) ---
    ('*', 1001, 'markerFuserUnderTemperature', 'Фьюзер: недогрев', None),
    ('*', 1002, 'markerFuserOverTemperature', 'Фьюзер: перегрев', None),
    ('*', 1003, 'markerFuserTimingFailure', 'Фьюзер: сбой синхронизации', None),
    ('*', 1004, 'markerFuserThermistorFailure', 'Фьюзер: отказ термистора', None),
    ('*', 1005, 'markerAdjustingPrintQuality', 'Регулировка качества печати', None),
    ('*', 1010, 'markerLifeAlmostOver', 'Ресурс маркера на исходе', None),
    ('*', 1011, 'markerLifeOver', 'Ресурс маркера исчерпан', None),
    ('*', 1013, 'markerMissing', 'Маркер отсутствует', None),
    ('*', 1014, 'markerMotorFailure', 'Отказ двигателя маркера', None),
    ('*', 1019, 'markerPowerSaver', 'Маркер: спящий режим', None),
    ('*', 1030, 'markerWarmingUp', 'Маркер: прогрев', None),
    # --- расходники (1101-1131) ---
    ('*', 1101, 'markerTonerEmpty', 'Тонер закончился', 'Заменить тонер'),
    ('*', 1102, 'markerInkEmpty', 'Чернила закончились', 'Заменить чернила'),
    ('*', 1103, 'markerPrintRibbonEmpty', 'Лента закончилась', None),
    ('*', 1104, 'markerTonerAlmostEmpty', 'Тонер заканчивается', 'Подготовить картридж'),
    ('*', 1105, 'markerInkAlmostEmpty', 'Чернила заканчиваются', None),
    ('*', 1106, 'markerPrintRibbonAlmostEmpty', 'Лента заканчивается', None),
    ('*', 1107, 'markerWasteTonerReceptacleAlmostFull', 'Бункер отработанного тонера почти полон', None),
    ('*', 1108, 'markerWasteInkReceptacleAlmostFull', 'Бункер отработанных чернил почти полон', None),
    ('*', 1109, 'markerWasteTonerReceptacleFull', 'Бункер отработанного тонера полон', 'Заменить/опустошить бункер'),
    ('*', 1110, 'markerWasteInkReceptacleFull', 'Бункер отработанных чернил полон', None),
    ('*', 1111, 'markerOpcLifeAlmostOver', 'Фотобарабан: ресурс на исходе', None),
    ('*', 1112, 'markerOpcLifeOver', 'Фотобарабан: ресурс исчерпан', 'Заменить фотобарабан'),
    ('*', 1113, 'markerDeveloperAlmostEmpty', 'Девелопер заканчивается', None),
    ('*', 1114, 'markerDeveloperEmpty', 'Девелопер закончился', None),
    ('*', 1115, 'markerTonerCartridgeMissing', 'Картридж с тонером отсутствует', 'Установить картридж'),
    ('*', 1116, 'markerCleanerMissing', 'Чистящий узел отсутствует', None),
    ('*', 1117, 'markerDeveloperMissing', 'Девелопер отсутствует', None),
    ('*', 1118, 'markerFuserMissing', 'Фьюзер отсутствует', None),
    ('*', 1119, 'markerInkMissing', 'Чернила отсутствуют', None),
    ('*', 1120, 'markerOpcMissing', 'Фотобарабан отсутствует', None),
    ('*', 1121, 'markerPrintRibbonMissing', 'Лента отсутствует', None),
    ('*', 1122, 'markerSupplyAlmostEmpty', 'Расходник заканчивается', None),
    ('*', 1123, 'markerSupplyEmpty', 'Расходник закончился', 'Заменить расходник'),
    ('*', 1124, 'markerSupplyMissing', 'Расходник отсутствует', None),
    ('*', 1125, 'markerWasteAlmostFull', 'Бункер отходов почти полон', None),
    ('*', 1126, 'markerWasteFull', 'Бункер отходов полон', None),
    ('*', 1127, 'markerWasteMissing', 'Бункер отходов отсутствует', None),
    ('*', 1128, 'markerWasteInkReceptacleMissing', 'Бункер отработанных чернил отсутствует', None),
    ('*', 1129, 'markerWasteTonerReceptacleMissing', 'Бункер отработанного тонера отсутствует', None),
    ('*', 1130, 'markerTonerMissing', 'Тонер отсутствует', 'Установить картридж'),
    ('*', 1131, 'markerSupplyFailure', 'Сбой расходника', None),
    # --- медиатракт (1301-1334) ---
    ('*', 1301, 'mediaPathMediaTrayMissing', 'Лоток тракта отсутствует', None),
    ('*', 1302, 'mediaPathMediaTrayAlmostFull', 'Лоток тракта почти полон', None),
    ('*', 1303, 'mediaPathMediaTrayFull', 'Лоток тракта полон', None),
    ('*', 1304, 'mediaPathCannotDuplexMediaSelected', 'Дуплекс невозможен для выбранной бумаги', None),
    ('*', 1305, 'mediaPathFailure', 'Неисправность медиатракта', None),
    ('*', 1306, 'mediaPathJam', 'Замятие в медиатракте', 'Устранить замятие'),
    ('*', 1310, 'mediaPathInputRequest', 'Требуется подача бумаги', None),
    ('*', 1311, 'mediaPathInputFeedError', 'Ошибка подачи на входе', None),
    ('*', 1312, 'mediaPathInputJam', 'Замятие на входе', 'Устранить замятие'),
    ('*', 1321, 'mediaPathOutputFeedError', 'Ошибка подачи на выходе', None),
    ('*', 1322, 'mediaPathOutputJam', 'Замятие на выходе', 'Устранить замятие'),
    ('*', 1323, 'mediaPathOutputFull', 'Выход переполнен', None),
    ('*', 1331, 'mediaPathPickRollerLifeWarn', 'Ролик тракта: ресурс на исходе', None),
    ('*', 1332, 'mediaPathPickRollerLifeOver', 'Ролик тракта: ресурс исчерпан', None),
    ('*', 1333, 'mediaPathPickRollerFailure', 'Отказ ролика тракта', None),
    ('*', 1334, 'mediaPathPickRollerMissing', 'Ролик тракта отсутствует', None),
    # --- интерпретатор (1501-1509) ---
    ('*', 1501, 'interpreterMemoryIncrease', 'Память увеличена', None),
    ('*', 1502, 'interpreterMemoryDecrease', 'Память уменьшена', None),
    ('*', 1503, 'interpreterCartridgeAdded', 'Картридж добавлен', None),
    ('*', 1504, 'interpreterCartridgeDeleted', 'Картридж извлечён', None),
    ('*', 1505, 'interpreterResourceAdded', 'Ресурс добавлен', None),
    ('*', 1506, 'interpreterResourceDeleted', 'Ресурс удалён', None),
    ('*', 1507, 'interpreterResourceUnavailable', 'Ресурс недоступен', None),
    ('*', 1509, 'interpreterComplexPageEncountered', 'Слишком сложная страница', None),
    # --- служебное (1801) ---
    ('*', 1801, 'alertRemovalOfBinaryChangeEntry', 'Событие снято', None),
    # --- сканер (5101-5114) ---
    ('*', 5101, 'scannerLightLifeAlmostOver', 'Лампа сканера: ресурс на исходе', None),
    ('*', 5102, 'scannerLightLifeOver', 'Лампа сканера: ресурс исчерпан', None),
    ('*', 5103, 'scannerLightFailure', 'Отказ лампы сканера', None),
    ('*', 5104, 'scannerLightMissing', 'Лампа сканера отсутствует', None),
    ('*', 5111, 'scannerSensorLifeAlmostOver', 'Датчик сканера: ресурс на исходе', None),
    ('*', 5112, 'scannerSensorLifeOver', 'Датчик сканера: ресурс исчерпан', None),
    ('*', 5113, 'scannerSensorFailure', 'Отказ датчика сканера', None),
    ('*', 5114, 'scannerSensorMissing', 'Датчик сканера отсутствует', None),
    # --- тракт сканера (5201-5234) ---
    ('*', 5201, 'scanMediaPathTrayMissing', 'Лоток сканера отсутствует', None),
    ('*', 5202, 'scanMediaPathTrayAlmostFull', 'Лоток сканера почти полон', None),
    ('*', 5203, 'scanMediaPathTrayFull', 'Лоток сканера полон', None),
    ('*', 5205, 'scanMediaPathFailure', 'Неисправность тракта сканера', None),
    ('*', 5206, 'scanMediaPathJam', 'Замятие в тракте сканера', 'Устранить замятие'),
    ('*', 5210, 'scanMediaPathInputRequest', 'Требуется подача оригинала', None),
    ('*', 5211, 'scanMediaPathInputFeedError', 'Ошибка подачи оригинала', None),
    ('*', 5212, 'scanMediaPathInputJam', 'Замятие на входе сканера', 'Устранить замятие'),
    ('*', 5221, 'scanMediaPathOutputFeedError', 'Ошибка вывода сканера', None),
    ('*', 5222, 'scanMediaPathOutputJam', 'Замятие на выходе сканера', 'Устранить замятие'),
    ('*', 5223, 'scanMediaPathOutputFull', 'Выход сканера переполнен', None),
    ('*', 5231, 'scanMediaPathPickRollerLifeWarn', 'Ролик сканера: ресурс на исходе', None),
    ('*', 5232, 'scanMediaPathPickRollerLifeOver', 'Ролик сканера: ресурс исчерпан', None),
    ('*', 5233, 'scanMediaPathPickRollerFailure', 'Отказ ролика сканера', None),
    ('*', 5234, 'scanMediaPathPickRollerMissing', 'Ролик сканера отсутствует', None),
    # --- факс-модем (6101-6105) ---
    ('*', 6101, 'faxModemMissing', 'Факс-модем отсутствует', None),
    ('*', 6102, 'faxModemLifeAlmostOver', 'Факс-модем: ресурс на исходе', None),
    ('*', 6103, 'faxModemLifeOver', 'Факс-модем: ресурс исчерпан', None),
    ('*', 6104, 'faxModemTurnedOn', 'Факс-модем включён', None),
    ('*', 6105, 'faxModemTurnedOff', 'Факс-модем выключен', None),
    # --- вендорские уточнения (при необходимости дополнять сюда) ---
    ('kyocera', 1001, 'markerFuserUnderTemperature', 'Фьюзер: недогрев (прогрев/сон)', 'Обычно проходит после прогрева'),
]

ALERT_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_codes (
    vendor TEXT NOT NULL DEFAULT '*',   -- '*' = общий стандарт RFC 3805
    code INTEGER NOT NULL,
    name TEXT,                          -- powerUp, jam, ...
    title_ru TEXT NOT NULL,             -- расшифровка
    hint_ru TEXT,                       -- что делать (опционально)
    PRIMARY KEY (vendor, code)
);
"""

# prtAlertSeverityLevel: other(1), critical(3), serious(4), warning(5)
SEVERITY_ICON = {'3': '🔴', '4': '🟠', '5': '🟡', '1': 'ℹ️'}


def ensure_alerts_db():
    """Создаём alerts.db и сидим коды, если таблица пустая."""
    db = sqlite3.connect(ALERT_DATABASE)
    db.executescript(ALERT_DB_SCHEMA)
    if db.execute('SELECT COUNT(*) FROM alert_codes').fetchone()[0] == 0:
        db.executemany(
            'INSERT OR IGNORE INTO alert_codes (vendor, code, name, title_ru, hint_ru) VALUES (?,?,?,?,?)',
            ALERT_CODES_SEED)
        db.commit()
    db.close()


def detect_vendor(*texts):
    """Вендор по модели принтера / sysDescr."""
    t = ' '.join(x for x in texts if x).lower()
    if 'kyocera' in t:
        return 'kyocera'
    if 'xerox' in t or 'workcentre' in t:
        return 'xerox'
    if 'brother' in t:
        return 'brother'
    if 'hp' in t or 'hewlett' in t or 'laserjet' in t:
        return 'hp'
    return '*'


def decode_alerts(adb, vendor, codes, severities, descriptions):
    """(код, важность, описание устройства) → список строк с расшифровкой.

    Приоритет текста: описание от самого принтера (Xerox его отдаёт),
    затем вендорская запись из alerts.db, затем общая по RFC 3805.
    Дубликаты (Kyocera шлёт один код дважды) схлопываются.
    """
    sev_by_idx = {oid.split('.')[-1]: val for oid, val in severities}
    desc_by_idx = {oid.split('.')[-1]: val for oid, val in descriptions}
    alerts = []
    seen = set()
    for oid, code in codes:
        idx = oid.split('.')[-1]
        row = adb.execute(
            'SELECT title_ru, hint_ru FROM alert_codes WHERE vendor = ? AND code = ?',
            (vendor, int(code) if code.isdigit() else -1)).fetchone()
        if not row:
            row = adb.execute(
                "SELECT title_ru, hint_ru FROM alert_codes WHERE vendor = '*' AND code = ?",
                (int(code) if code.isdigit() else -1,)).fetchone()
        title = row[0] if row else 'Неизвестный код'
        hint = row[1] if row else None
        text = f'{code}: {title}'
        dev_desc = (desc_by_idx.get(idx) or '').strip()
        if dev_desc:
            if len(dev_desc) > 150:  # обрезаем по границе слова
                dev_desc = dev_desc[:150].rsplit(' ', 1)[0] + '…'
            text += ' — ' + dev_desc
        if hint:
            text += f' ({hint})'
        icon = SEVERITY_ICON.get(sev_by_idx.get(idx, ''), '⚠️')
        line = f'{icon} {text}'
        if line not in seen:
            seen.add(line)
            alerts.append(line)
    return alerts


def color_from_desc(desc):
    """Цвет картриджа по SNMP-описанию: 'Black Toner', 'TK-5380C', 'CF411A Cyan'.

    Возвращает 'black'/'cyan'/'magenta'/'yellow' или None для не-тонера
    (waste box, драм, фьюзер и т.п.). По умолчанию — 'black'
    (моно-принтеры без цвета в описании).
    """
    d = (desc or '').strip().lower()
    if not d:
        return None
    head = re.split(r'[;,]', d)[0].strip()  # отрезаем PN/SN-хвосты (Xerox)
    if any(x in head for x in ('waste', 'drum', 'fuser', 'belt', 'transfer', 'roller')):
        return None
    if 'black' in head or 'bk' in head or re.search(r'\bk\b', head):
        return 'black'
    if 'cyan' in head or re.search(r'\bc\b', head):
        return 'cyan'
    if 'magenta' in head or re.search(r'\bm\b', head):
        return 'magenta'
    if 'yellow' in head or re.search(r'\by\b', head):
        return 'yellow'
    # буква цвета на конце модели картриджа: TK-5380C / TK-8335K
    m = re.search(r'([kcmy])$', head)
    if m:
        return {'k': 'black', 'c': 'cyan', 'm': 'magenta', 'y': 'yellow'}[m.group(1)]
    return 'black'


def snmp_get_sync(ip, oid, community='public', timeout=3):
    """Синхронный SNMP GET. Возвращает строку или None."""
    try:
        for (errInd, errSts, errIdx, varBinds) in getCmd(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            UdpTransportTarget((ip, 161), timeout=timeout, retries=1),
            ContextData(),
            ObjectType(ObjectIdentity(oid))
        ):
            if errInd or errSts:
                return None
            for _, val in varBinds:
                return str(val)
    except Exception:
        return None
    return None


def snmp_walk_sync(ip, base_oid, community='public', timeout=3):
    """Синхронный SNMP WALK. Возвращает список (oid, value)."""
    results = []
    try:
        for (errInd, errSts, errIdx, varBinds) in nextCmd(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            UdpTransportTarget((ip, 161), timeout=timeout, retries=1),
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False
        ):
            if errInd or errSts:
                break
            for oid, val in varBinds:
                oid_str = str(oid)
                if not oid_str.startswith(base_oid):
                    return results
                results.append((oid_str, str(val)))
    except Exception:
        pass
    return results


def poll_all_printers():
    """Опрос всех принтеров с IP и запись в snmp_readings."""
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    printers = db.execute(
        'SELECT * FROM printers WHERE ip_address IS NOT NULL').fetchall()

    for p in printers:
        ip = p['ip_address']
        pid = p['id']
        try:
            # базовая информация
            sys_name = snmp_get_sync(ip, '1.3.6.1.2.1.1.5.0')
            sys_descr = snmp_get_sync(ip, '1.3.6.1.2.1.1.1.0')
            sys_status = snmp_get_sync(ip, '1.3.6.1.2.1.25.3.2.1.5.1')

            if sys_name is None and sys_descr is None:
                # принтер не отвечает (SNMP выключен / недоступен) — остаётся серым
                continue

            status_map = {'1': 'Running', '2': 'Warning', '3': 'Testing',
                          '4': 'Down', '5': 'NotPresent'}
            status_text = status_map.get(sys_status, sys_status or 'Unknown')

            # уровни тонера (стандартный Printer-MIB)
            levels = snmp_walk_sync(ip, '1.3.6.1.2.1.43.11.1.1.9')
            descriptions = snmp_walk_sync(ip, '1.3.6.1.2.1.43.11.1.1.6')
            maxcaps = snmp_walk_sync(ip, '1.3.6.1.2.1.43.11.1.1.8')

            black_level = cyan_level = magenta_level = yellow_level = None

            if levels:
                desc_map = {}
                for oid, val in descriptions:
                    desc_map[oid.split('.')[-1]] = val.lower()
                max_map = {}
                for oid, val in maxcaps:
                    max_map[oid.split('.')[-1]] = val

                for oid, val in levels:
                    idx = oid.split('.')[-1]
                    color = color_from_desc(desc_map.get(idx, ''))
                    if color is None:
                        continue
                    maxcap = max_map.get(idx, '0')
                    try:
                        level = int(val)
                        maxv = int(maxcap) if maxcap and int(maxcap) > 0 else 100
                        if maxv > 0 and level >= 0:
                            pct = round(level / maxv * 100)
                        else:
                            pct = level if level >= 0 else None
                    except ValueError:
                        continue
                    if pct is None:
                        continue
                    if color == 'black':
                        black_level = pct
                    elif color == 'cyan':
                        cyan_level = pct
                    elif color == 'magenta':
                        magenta_level = pct
                    elif color == 'yellow':
                        yellow_level = pct

            # счётчик страниц
            page_counter = snmp_get_sync(ip, '1.3.6.1.2.1.43.10.2.1.4.1.1')
            try:
                page_counter = int(page_counter) if page_counter else None
            except ValueError:
                page_counter = None

            # алерты: код (.7) + важность (.2) + описание устройства (.8)
            adb = sqlite3.connect(ALERT_DATABASE)
            try:
                vendor = detect_vendor(p['model'], p['name'], sys_descr)
                alerts = decode_alerts(
                    adb, vendor,
                    snmp_walk_sync(ip, '1.3.6.1.2.1.43.18.1.1.7'),
                    snmp_walk_sync(ip, '1.3.6.1.2.1.43.18.1.1.2'),
                    snmp_walk_sync(ip, '1.3.6.1.2.1.43.18.1.1.8'))
            finally:
                adb.close()

            db.execute(
                '''INSERT INTO snmp_readings
                   (printer_id, black_level, cyan_level, magenta_level, yellow_level,
                    page_counter, status_text, alerts, raw_data)
                   VALUES (?,?,?,?,?,?,?,?,?)''',
                (pid, black_level, cyan_level, magenta_level, yellow_level,
                 page_counter, status_text, json.dumps(alerts, ensure_ascii=False),
                 json.dumps({'sys_name': sys_name, 'sys_descr': sys_descr},
                            ensure_ascii=False)))
            db.commit()
        except Exception:
            # любая ошибка по одному принтеру не должна ронять опрос остальных
            continue

    db.close()


def start_snmp_polling():
    """Фоновый опрос раз в SNMP_INTERVAL секунд.

    Весь цикл живёт в отдельном daemon-потоке, чтобы опрос (а особенно
    таймауты недоступных принтеров) не блокировал старт Flask.
    """
    def _loop():
        while True:
            try:
                poll_all_printers()
            except Exception:
                pass
            time.sleep(SNMP_INTERVAL)

    threading.Thread(target=_loop, daemon=True).start()


# ------------------------------------------------------------------ Утилиты

def row_to_dict(row):
    return dict(row) if row is not None else None


def get_barcode(db, ean):
    row = db.execute('SELECT * FROM barcode_map WHERE ean_13 = ?', (ean,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d['compatible_printers'] = json.loads(d['compatible_printers'] or '[]')
    except (ValueError, TypeError):
        d['compatible_printers'] = []
    return d


def toner_with_info(db, toner):
    """Дополняем запись тонера данными модели."""
    d = dict(toner)
    bc = get_barcode(db, d['ean_13'])
    d['model_name'] = bc['model_name'] if bc else None
    d['color'] = bc['color'] if bc else None
    d['page_yield'] = bc['page_yield'] if bc else None
    return d


def printer_status(db, printer):
    """Цвет статуса принтера: green/yellow/red/grey.

    Принтер с ip_address — по последним SNMP-данным (уровень тонера:
    ≤5% красный, ≤20% жёлтый, нет данных — серый). Принтер без IP —
    по старой логике (заполненность слотов и возраст тонера).
    """
    if printer['ip_address']:
        row = db.execute(
            'SELECT black_level, cyan_level, magenta_level, yellow_level '
            'FROM snmp_readings WHERE printer_id = ? ORDER BY timestamp DESC LIMIT 1',
            (printer['id'],)).fetchone()
        if not row:
            return 'grey'  # не отвечает или ещё не опрошен
        if printer['type'] == 'mono':
            level = row['black_level']
            if level is None:
                return 'grey'
            if level <= 5:
                return 'red'
            if level <= 20:
                return 'yellow'
            return 'green'
        levels = [l for l in (row['black_level'], row['cyan_level'],
                              row['magenta_level'], row['yellow_level'])
                  if l is not None]
        if not levels:
            return 'grey'
        if any(l <= 5 for l in levels):
            return 'red'
        if any(l <= 20 for l in levels):
            return 'yellow'
        return 'green'
    # --- принтер без IP: старая логика по слотам ---
    if printer['type'] == 'color':
        slots = ['toner_bk_id', 'toner_c_id', 'toner_m_id', 'toner_y_id']
    else:
        slots = ['toner_bk_id']
    any_data = False
    aging = False
    now = datetime.now()
    for col in slots:
        tid = printer[col]
        if not tid:
            return 'red'  # обязательный слот пуст
        any_data = True
        toner = db.execute('SELECT * FROM toners WHERE id = ?', (tid,)).fetchone()
        if toner and toner['installed_at']:
            bc = get_barcode(db, toner['ean_13'])
            if bc and bc['page_yield']:
                try:
                    inst = datetime.fromisoformat(str(toner['installed_at']))
                    if now - inst > timedelta(days=AGING_DAYS):
                        aging = True
                except ValueError:
                    pass
    if not any_data:
        return 'grey'
    return 'yellow' if aging else 'green'


def serialize_printer(db, printer):
    d = dict(printer)
    d['status_color'] = printer_status(db, printer)
    for col, key in [('toner_bk_id', 'slot_bk'), ('toner_c_id', 'slot_c'),
                     ('toner_m_id', 'slot_m'), ('toner_y_id', 'slot_y')]:
        d[key] = None
        if d[col]:
            toner = db.execute('SELECT * FROM toners WHERE id = ?', (d[col],)).fetchone()
            if toner:
                d[key] = toner_with_info(db, toner)
    return d


# ------------------------------------------------------------------ Страницы

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/scan')
def scan():
    return render_template('scan.html')


@app.route('/item/<ean>')
def item(ean):
    return render_template('item.html', ean=ean)


@app.route('/admin')
def admin():
    return render_template('admin.html')


@app.route('/history')
def history():
    return render_template('history.html')


@app.route('/stats')
def stats():
    return render_template('stats.html')


@app.route('/availability')
def availability():
    return render_template('availability.html')


@app.route('/admin/qr_print')
def qr_print():
    db = get_db()
    rows = db.execute('SELECT ean_13, model_name, color FROM barcode_map ORDER BY model_name').fetchall()
    items = []
    for r in rows:
        png = os.path.join(QR_DIR, f'{r["ean_13"]}.png')
        if not os.path.exists(png):
            make_qr(r['ean_13'])
        items.append({'ean_13': r['ean_13'], 'model_name': r['model_name'],
                      'color': r['color'], 'qr_url': url_for('static', filename=f'qr_codes/{r["ean_13"]}.png')})
    return render_template('qr_print.html', items=items)


# ------------------------------------------------------------------ JSON API

@app.route('/api/lookup/<ean>')
def api_lookup(ean):
    db = get_db()
    bc = get_barcode(db, ean)
    if not bc:
        return jsonify({'found': False, 'ean_13': ean})
    # EAN общий для всех тонеров модели+цвета: считаем остаток на складе,
    # отдельно находим установленный экземпляр (если есть)
    stock_rows = db.execute(
        "SELECT id FROM toners WHERE ean_13 = ? AND status = 'stock' ORDER BY id",
        (ean,)).fetchall()
    installed = db.execute(
        "SELECT * FROM toners WHERE ean_13 = ? AND status = 'installed' ORDER BY id DESC LIMIT 1",
        (ean,)).fetchone()
    resp = {'found': True, 'barcode': bc, 'toner': None, 'printer': None,
            'stock_count': len(stock_rows),
            'stock_toner_id': stock_rows[-1]['id'] if stock_rows else None}
    if installed:
        resp['toner'] = toner_with_info(db, installed)
        if installed['current_printer_id']:
            p = db.execute('SELECT * FROM printers WHERE id = ?',
                           (installed['current_printer_id'],)).fetchone()
            resp['printer'] = row_to_dict(p)
    return jsonify(resp)


@app.route('/api/lookup_by_id/<int:tid>')
def api_lookup_by_id(tid):
    """Тонер по id + его запись из barcode_map (для режима выбора на карте)."""
    db = get_db()
    toner = db.execute('SELECT * FROM toners WHERE id = ?', (tid,)).fetchone()
    if not toner:
        return jsonify({'error': 'Тонер не найден'}), 404
    return jsonify({'toner': toner_with_info(db, toner), 'barcode': get_barcode(db, toner['ean_13'])})


@app.route('/api/barcode_map', methods=['POST'])
def api_barcode_map_create():
    """Обучение нового EAN: запись в barcode_map + тонеры на склад (quantity шт.)."""
    data = request.get_json(force=True)
    ean = (data.get('ean_13') or '').strip()
    if not ean:
        return jsonify({'error': 'ean_13 обязателен'}), 400
    qty = int(data.get('quantity') or 1)
    if qty < 1:
        return jsonify({'error': 'Количество должно быть ≥ 1'}), 400
    compat = data.get('compatible_printers') or []
    if isinstance(compat, str):
        compat = [compat]
    db = get_db()
    db.execute(
        'INSERT OR REPLACE INTO barcode_map (ean_13, model_name, color, compatible_printers, page_yield) VALUES (?,?,?,?,?)',
        (ean, data.get('model_name'), data.get('color'),
         json.dumps(compat, ensure_ascii=False), data.get('page_yield')))
    _add_stock(db, ean, qty)
    db.commit()
    return jsonify({'ok': True, 'barcode': get_barcode(db, ean)})


def _add_stock(db, ean, qty):
    """Приход qty штук на склад + запись в operations."""
    for _ in range(qty):
        cur = db.execute("INSERT INTO toners (ean_13, status) VALUES (?, 'stock')", (ean,))
        db.execute(
            "INSERT INTO operations (toner_id, printer_id, type, old_toner_id) VALUES (?,?,?,?)",
            (cur.lastrowid, None, 'stock_add', None))


@app.route('/api/stock/add', methods=['POST'])
def api_stock_add():
    """Приход на склад: qty штук по известному EAN (код общий для модели+цвета)."""
    data = request.get_json(force=True)
    ean = (data.get('ean_13') or '').strip()
    qty = int(data.get('quantity') or 0)
    if qty < 1:
        return jsonify({'error': 'Количество должно быть ≥ 1'}), 400
    db = get_db()
    if not get_barcode(db, ean):
        return jsonify({'error': 'EAN не найден в справочнике'}), 404
    _add_stock(db, ean, qty)
    db.commit()
    return jsonify({'ok': True, 'added': qty})


@app.route('/api/stock/deplete', methods=['POST'])
def api_stock_deplete():
    """Ручное списание qty штук со склада по EAN (списываются самые старые)."""
    data = request.get_json(force=True)
    ean = (data.get('ean_13') or '').strip()
    qty = int(data.get('quantity') or 0)
    if qty < 1:
        return jsonify({'error': 'Количество должно быть ≥ 1'}), 400
    db = get_db()
    rows = db.execute(
        "SELECT id FROM toners WHERE ean_13 = ? AND status = 'stock' ORDER BY id",
        (ean,)).fetchall()
    if len(rows) < qty:
        return jsonify({'error': f'На складе только {len(rows)} шт.'}), 400
    for r in rows[:qty]:
        db.execute("UPDATE toners SET status='depleted' WHERE id=?", (r['id'],))
        db.execute(
            "INSERT INTO operations (toner_id, printer_id, type, old_toner_id) VALUES (?,?,?,?)",
            (r['id'], None, 'depleted', None))
    db.commit()
    return jsonify({'ok': True, 'depleted': qty})


@app.route('/api/install', methods=['POST'])
def api_install():
    """Установка тонера в принтер; старый тонер слота автоматически списывается."""
    data = request.get_json(force=True)
    toner_id = data.get('toner_id')
    printer_id = data.get('printer_id')
    db = get_db()
    toner = db.execute('SELECT * FROM toners WHERE id = ?', (toner_id,)).fetchone()
    printer = db.execute('SELECT * FROM printers WHERE id = ?', (printer_id,)).fetchone()
    if not toner or not printer:
        return jsonify({'error': 'Тонер или принтер не найден'}), 404
    if toner['status'] == 'installed':
        return jsonify({'error': 'Тонер уже установлен'}), 400
    bc = get_barcode(db, toner['ean_13'])
    if not bc or bc['color'] not in SLOT_COLUMN:
        return jsonify({'error': 'Неизвестный цвет тонера'}), 400
    col = SLOT_COLUMN[bc['color']]  # цвет всегда из БД, вручную не выбирается
    if printer['type'] == 'mono' and col != 'toner_bk_id':
        return jsonify({'error': 'В моно-принтер можно ставить только чёрный тонер'}), 400

    old_toner_id = printer[col]
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if old_toner_id:
        # старый тонер из этого слота → списан
        db.execute("UPDATE toners SET status='depleted', current_printer_id=NULL WHERE id=?",
                   (old_toner_id,))
        db.execute(
            "INSERT INTO operations (toner_id, printer_id, type, old_toner_id) VALUES (?,?,?,?)",
            (old_toner_id, printer_id, 'auto_depleted', None))
    db.execute(
        "UPDATE toners SET status='installed', current_printer_id=?, installed_at=? WHERE id=?",
        (printer_id, now, toner_id))
    db.execute(f'UPDATE printers SET {col}=? WHERE id=?', (toner_id, printer_id))
    db.execute(
        "INSERT INTO operations (toner_id, printer_id, type, old_toner_id) VALUES (?,?,?,?)",
        (toner_id, printer_id, 'install', old_toner_id))
    db.commit()
    p = db.execute('SELECT * FROM printers WHERE id = ?', (printer_id,)).fetchone()
    return jsonify({'ok': True, 'auto_depleted_id': old_toner_id,
                    'printer': serialize_printer(db, p)})


@app.route('/api/return', methods=['POST'])
def api_return():
    """Возврат тонера на склад, слот принтера освобождается."""
    data = request.get_json(force=True)
    toner_id = data.get('toner_id')
    db = get_db()
    toner = db.execute('SELECT * FROM toners WHERE id = ?', (toner_id,)).fetchone()
    if not toner:
        return jsonify({'error': 'Тонер не найден'}), 404
    if toner['status'] != 'installed':
        return jsonify({'error': 'Тонер не установлен в принтер'}), 400
    printer_id = toner['current_printer_id']
    if printer_id:
        for col in SLOT_COLUMN.values():
            db.execute(f'UPDATE printers SET {col}=NULL WHERE id=? AND {col}=?',
                       (printer_id, toner_id))
    db.execute(
        "UPDATE toners SET status='stock', current_printer_id=NULL, installed_at=NULL WHERE id=?",
        (toner_id,))
    db.execute(
        "INSERT INTO operations (toner_id, printer_id, type, old_toner_id) VALUES (?,?,?,?)",
        (toner_id, printer_id, 'return', None))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/printers', methods=['GET'])
def api_printers_list():
    db = get_db()
    fid = request.args.get('floor_id', type=int)
    if fid:
        rows = db.execute('SELECT * FROM printers WHERE floor_id = ? ORDER BY id', (fid,)).fetchall()
    else:
        rows = db.execute('SELECT * FROM printers ORDER BY id').fetchall()
    return jsonify([serialize_printer(db, p) for p in rows])


@app.route('/api/printers', methods=['POST'])
def api_printers_create():
    data = request.get_json(force=True)
    ptype = data.get('type') or 'mono'
    db = get_db()
    floor_id = data.get('floor_id')
    if not floor_id:
        floor_id = db.execute('SELECT MIN(id) FROM floor_plans').fetchone()[0]
    cur = db.execute(
        'INSERT INTO printers (name, model, type, slots_count, x, y, ip_address, floor_id) VALUES (?,?,?,?,?,?,?,?)',
        (data.get('name'), data.get('model'), ptype,
         4 if ptype == 'color' else 1,
         float(data.get('x') or 50), float(data.get('y') or 50),
         data.get('ip_address'), floor_id))
    db.commit()
    p = db.execute('SELECT * FROM printers WHERE id = ?', (cur.lastrowid,)).fetchone()
    return jsonify(serialize_printer(db, p)), 201


@app.route('/api/printers/<int:pid>', methods=['PUT'])
def api_printers_update(pid):
    data = request.get_json(force=True)
    db = get_db()
    p = db.execute('SELECT * FROM printers WHERE id = ?', (pid,)).fetchone()
    if not p:
        return jsonify({'error': 'Принтер не найден'}), 404
    fields = {}
    for key in ('name', 'model', 'type', 'ip_address'):
        if key in data:
            fields[key] = data[key]
    if 'floor_id' in data:
        fields['floor_id'] = int(data['floor_id'])
    for key in ('x', 'y'):
        if key in data:
            fields[key] = float(data[key])
    if 'type' in fields:
        fields['slots_count'] = 4 if fields['type'] == 'color' else 1
    if fields:
        sets = ', '.join(f'{k}=?' for k in fields)
        db.execute(f'UPDATE printers SET {sets} WHERE id=?', (*fields.values(), pid))
        db.commit()
    p = db.execute('SELECT * FROM printers WHERE id = ?', (pid,)).fetchone()
    return jsonify(serialize_printer(db, p))


@app.route('/api/printers/<int:pid>', methods=['DELETE'])
def api_printers_delete(pid):
    db = get_db()
    db.execute('DELETE FROM printers WHERE id = ?', (pid,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/printers/<int:pid>/available_toners')
def api_printer_available_toners(pid):
    """Совместимые тонеры, которые сейчас есть на складе (для замены с карты).

    Для каждого подходящего EAN отдаём id складского экземпляра
    (для /api/install) и остаток в штуках.
    """
    db = get_db()
    p = db.execute('SELECT * FROM printers WHERE id = ?', (pid,)).fetchone()
    if not p:
        return jsonify({'error': 'Принтер не найден'}), 404
    out = []
    for r in db.execute('SELECT ean_13 FROM barcode_map'):
        bc = get_barcode(db, r['ean_13'])
        if not bc or p['model'] not in (bc['compatible_printers'] or []):
            continue
        if p['type'] == 'mono' and bc['color'] != 'Black':
            continue
        stock = db.execute(
            "SELECT id FROM toners WHERE ean_13 = ? AND status = 'stock' ORDER BY id",
            (bc['ean_13'],)).fetchall()
        if not stock:
            continue
        out.append({'toner_id': stock[-1]['id'], 'ean_13': bc['ean_13'],
                    'model_name': bc['model_name'], 'color': bc['color'],
                    'stock_count': len(stock)})
    return jsonify(out)


@app.route('/api/printers/<int:pid>/snmp')
def api_printer_snmp(pid):
    """Последние SNMP-данные конкретного принтера."""
    db = get_db()
    row = db.execute(
        'SELECT * FROM snmp_readings WHERE printer_id = ? ORDER BY timestamp DESC LIMIT 1',
        (pid,)).fetchone()
    if not row:
        return jsonify({'error': 'Нет данных'}), 404
    d = dict(row)
    d['alerts'] = json.loads(d['alerts'] or '[]')
    d['raw_data'] = json.loads(d['raw_data'] or '{}')
    return jsonify(d)


@app.route('/api/printers/snmp/all')
def api_all_printers_snmp():
    """Последние SNMP-данные всех принтеров (для карты/дашборда)."""
    db = get_db()
    rows = db.execute('''
        SELECT r.*, p.name AS printer_name, p.type
        FROM snmp_readings r
        JOIN (
            SELECT printer_id, MAX(timestamp) AS max_ts
            FROM snmp_readings GROUP BY printer_id
        ) latest ON r.printer_id = latest.printer_id AND r.timestamp = latest.max_ts
        JOIN printers p ON p.id = r.printer_id
    ''').fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['alerts'] = json.loads(d['alerts'] or '[]')
        result.append(d)
    return jsonify(result)


@app.route('/api/barcode_map/<ean>', methods=['PUT'])
def api_barcode_map_update(ean):
    data = request.get_json(force=True)
    db = get_db()
    if not db.execute('SELECT 1 FROM barcode_map WHERE ean_13=?', (ean,)).fetchone():
        return jsonify({'error': 'EAN не найден'}), 404
    compat = data.get('compatible_printers') or []
    if isinstance(compat, str):
        compat = [compat]
    db.execute(
        'UPDATE barcode_map SET model_name=?, color=?, compatible_printers=?, page_yield=? WHERE ean_13=?',
        (data.get('model_name'), data.get('color'),
         json.dumps(compat, ensure_ascii=False), data.get('page_yield'), ean))
    db.commit()
    return jsonify({'ok': True, 'barcode': get_barcode(db, ean)})


@app.route('/api/barcode_map/<ean>', methods=['DELETE'])
def api_barcode_map_delete(ean):
    db = get_db()
    db.execute('DELETE FROM barcode_map WHERE ean_13 = ?', (ean,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/barcode_map', methods=['GET'])
def api_barcode_map_list():
    db = get_db()
    rows = db.execute('SELECT * FROM barcode_map ORDER BY model_name').fetchall()
    return jsonify([get_barcode(db, r['ean_13']) for r in rows])


ALLOWED_PLAN_EXT = {'.jpg', '.jpeg', '.png', '.webp'}


def plan_json(row):
    """Этаж → JSON с URL картинки."""
    rel = row['image_path']
    if rel.startswith('static/'):
        rel = rel[len('static/'):]
    return {'id': row['id'], 'name': row['name'],
            'image_url': url_for('static_from_root', path=rel)}


@app.route('/api/floor_plans', methods=['GET'])
def api_floor_plans_list():
    """Список всех этажей (5 и 9) с картинками."""
    db = get_db()
    rows = db.execute('SELECT * FROM floor_plans ORDER BY id').fetchall()
    return jsonify([plan_json(r) for r in rows])


@app.route('/api/floor_plan', methods=['POST'])
def api_floor_plan_upload():
    """Загрузка плана (JPEG/PNG) на конкретный этаж (поле floor_id)."""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'Файл не выбран'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_PLAN_EXT:
        return jsonify({'error': 'Нужен JPEG или PNG файл'}), 400
    db = get_db()
    fid = request.form.get('floor_id', type=int)
    if fid:
        plan = db.execute('SELECT * FROM floor_plans WHERE id = ?', (fid,)).fetchone()
    else:
        plan = db.execute('SELECT * FROM floor_plans ORDER BY id LIMIT 1').fetchone()
    if not plan:
        return jsonify({'error': 'Этаж не найден'}), 404
    rel = f'static/floor_plan_{plan["id"]}_{datetime.now().strftime("%Y%m%d%H%M%S")}{ext}'
    f.save(os.path.join(BASE_DIR, rel))
    db.execute('UPDATE floor_plans SET image_path = ? WHERE id = ?', (rel, plan['id']))
    db.commit()
    row = db.execute('SELECT * FROM floor_plans WHERE id = ?', (plan['id'],)).fetchone()
    resp = plan_json(row)
    resp['ok'] = True
    resp['image_path'] = resp['image_url']  # совместимость со старым фронтом
    return jsonify(resp)


# отдача файлов из static/ (в т.ч. загруженных планов)
@app.route('/static/<path:path>')
def static_from_root(path):
    return send_from_directory(STATIC_DIR, path)


@app.route('/api/floor_plan', methods=['GET'])
def api_floor_plan_active():
    """План конкретного этажа (?floor_id=), по умолчанию — первый."""
    db = get_db()
    fid = request.args.get('floor_id', type=int)
    if fid:
        row = db.execute('SELECT * FROM floor_plans WHERE id = ?', (fid,)).fetchone()
    else:
        row = db.execute('SELECT * FROM floor_plans ORDER BY id LIMIT 1').fetchone()
    if not row:
        return jsonify({'image_url': url_for('static', filename='floor_plan.png')})
    return jsonify(plan_json(row))


def make_qr(ean):
    """Генерация PNG QR-кода, кодирующего EAN."""
    os.makedirs(QR_DIR, exist_ok=True)
    path = os.path.join(QR_DIR, f'{ean}.png')
    img = qrcode.make(ean)
    img.save(path)
    return path


@app.route('/api/qr/<ean>')
def api_qr(ean):
    make_qr(ean)
    return jsonify({'ok': True, 'url': url_for('static_from_root', path=f'qr_codes/{ean}.png')})


@app.route('/api/stats')
def api_stats():
    db = get_db()
    # замены по месяцам
    per_month = db.execute(
        "SELECT strftime('%Y-%m', timestamp) AS m, COUNT(*) AS c FROM operations "
        "WHERE type='install' GROUP BY m ORDER BY m").fetchall()
    # средний срок жизни тонера по моделям (install -> auto_depleted)
    # v2: предиктивная аналитика по page_yield (остаток ресурса, прогноз даты замены)
    lifespan = db.execute(
        """
        SELECT bm.model_name, AVG(julianday(d.timestamp) - julianday(i.timestamp)) AS avg_days, COUNT(*) AS n
        FROM operations d
        JOIN operations i ON i.toner_id = d.toner_id AND i.type = 'install'
        JOIN toners t ON t.id = d.toner_id
        JOIN barcode_map bm ON bm.ean_13 = t.ean_13
        WHERE d.type = 'auto_depleted'
        GROUP BY bm.model_name
        """).fetchall()
    # топ принтеров по числу замен
    top_printers = db.execute(
        "SELECT p.name, COUNT(*) AS c FROM operations o JOIN printers p ON p.id = o.printer_id "
        "WHERE o.type='install' GROUP BY p.name ORDER BY c DESC LIMIT 10").fetchall()
    # склад по моделям
    stock = db.execute(
        "SELECT bm.model_name, bm.color, COUNT(*) AS c FROM toners t "
        "JOIN barcode_map bm ON bm.ean_13 = t.ean_13 "
        "WHERE t.status='stock' GROUP BY bm.model_name, bm.color ORDER BY bm.model_name").fetchall()
    return jsonify({
        'replacements_per_month': [{'month': r['m'], 'count': r['c']} for r in per_month],
        'lifespan_by_model': [{'model': r['model_name'], 'avg_days': round(r['avg_days'], 1) if r['avg_days'] else 0,
                               'samples': r['n']} for r in lifespan],
        'top_printers': [{'name': r['name'], 'count': r['c']} for r in top_printers],
        'stock_by_model': [{'model': r['model_name'], 'color': r['color'], 'count': r['c']} for r in stock],
    })


def query_history(args):
    db = get_db()
    sql = (
        "SELECT o.*, t.ean_13, bm.model_name, bm.color, p.name AS printer_name "
        "FROM operations o "
        "LEFT JOIN toners t ON t.id = o.toner_id "
        "LEFT JOIN barcode_map bm ON bm.ean_13 = t.ean_13 "
        "LEFT JOIN printers p ON p.id = o.printer_id WHERE 1=1")
    params = []
    if args.get('date_from'):
        sql += ' AND o.timestamp >= ?'
        params.append(args['date_from'] + ' 00:00:00')
    if args.get('date_to'):
        sql += ' AND o.timestamp <= ?'
        params.append(args['date_to'] + ' 23:59:59')
    if args.get('printer_id'):
        sql += ' AND o.printer_id = ?'
        params.append(args['printer_id'])
    if args.get('type'):
        sql += ' AND o.type = ?'
        params.append(args['type'])
    sql += ' ORDER BY o.timestamp DESC, o.id DESC'
    return db.execute(sql, params).fetchall()


@app.route('/api/history')
def api_history():
    rows = query_history(request.args)
    return jsonify([dict(r) for r in rows])


@app.route('/history/export')
def history_export():
    rows = query_history(request.args)
    wb = Workbook()
    ws = wb.active
    ws.title = 'История операций'
    ws.append(['Дата/время', 'Тип', 'Модель тонера', 'EAN-13', 'Цвет',
               'Принтер', 'ID старого тонера'])
    type_ru = {'install': 'Установка', 'return': 'Возврат на склад',
               'auto_depleted': 'Авто-списание', 'stock_add': 'Приход на склад',
               'depleted': 'Списание'}
    for r in rows:
        ws.append([r['timestamp'], type_ru.get(r['type'], r['type']),
                   r['model_name'], r['ean_13'], r['color'],
                   r['printer_name'], r['old_toner_id']])
    for col_cells in ws.columns:
        width = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells) + 2
        ws.column_dimensions[col_cells[0].column_letter].width = min(width, 40)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f'toner_history_{date.today().isoformat()}.xlsx'
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/availability')
def api_availability():
    """Наличие тонеров на складе сгруппировано по моделям принтеров.

    Принтеры с разными именами, но одинаковой моделью объединяются в одну
    группу. Для каждого цвета суммируются остатки всех EAN (в т.ч. аналогов
    других производителей), у которых модель принтера есть в compatible_printers.
    """
    db = get_db()
    stock_by_ean = {r['ean_13']: r['c'] for r in db.execute(
        "SELECT ean_13, COUNT(*) AS c FROM toners WHERE status='stock' GROUP BY ean_13")}
    bcs = [get_barcode(db, r['ean_13'])
           for r in db.execute('SELECT ean_13 FROM barcode_map')]
    groups = {}
    for p in db.execute('SELECT * FROM printers ORDER BY name'):
        g = groups.setdefault(p['model'], {
            'model': p['model'], 'type': p['type'], 'printers': [], 'colors': {}})
        g['printers'].append(p['name'])
        if p['type'] == 'color':
            g['type'] = 'color'
    for g in groups.values():
        needed = ['Black'] if g['type'] == 'mono' else list(SLOT_COLUMN)
        colors = {c: 0 for c in needed}
        for bc in bcs:
            if not bc or bc['color'] not in colors:
                continue
            if g['model'] in (bc['compatible_printers'] or []):
                colors[bc['color']] += stock_by_ean.get(bc['ean_13'], 0)
        g['colors'] = colors
    return jsonify(sorted(groups.values(), key=lambda g: g['model'] or ''))


@app.route('/api/stock')
def api_stock():
    db = get_db()
    rows = db.execute(
        "SELECT bm.model_name, bm.color, COUNT(*) AS c FROM toners t "
        "JOIN barcode_map bm ON bm.ean_13 = t.ean_13 "
        "WHERE t.status='stock' GROUP BY bm.model_name, bm.color ORDER BY bm.model_name").fetchall()
    return jsonify([{'model': r['model_name'], 'color': r['color'], 'count': r['c']} for r in rows])


@app.route('/stock/export')
def stock_export():
    db = get_db()
    rows = db.execute(
        "SELECT bm.model_name, bm.color, COUNT(*) AS c FROM toners t "
        "JOIN barcode_map bm ON bm.ean_13 = t.ean_13 "
        "WHERE t.status='stock' GROUP BY bm.model_name, bm.color ORDER BY bm.model_name").fetchall()
    wb = Workbook()
    ws = wb.active
    ws.title = 'Остатки на складе'
    ws.append(['Модель тонера', 'Цвет', 'Количество, шт.'])
    for r in rows:
        ws.append([r['model_name'], r['color'], r['c']])
    for col_cells in ws.columns:
        width = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells) + 2
        ws.column_dimensions[col_cells[0].column_letter].width = min(width, 40)
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f'stock_{date.today().isoformat()}.xlsx'
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ------------------------------------------------------------------ Запуск

init_db()
ensure_floor_plan()

if __name__ == '__main__':
    import os, socket

    # IP, для которого создан сертификат (setup-https сохраняет его в last_ip.txt)
    ip_file = os.path.join(BASE_DIR, 'last_ip.txt')
    if os.path.exists(ip_file):
        with open(ip_file, 'r', encoding='utf-8-sig') as f:
            local_ip = f.read().strip()
        if not local_ip:
            local_ip = None
    else:
        local_ip = None

    # Fallback: авто-определение текущего IP
    if not local_ip:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            local_ip = s.getsockname()[0]
        except Exception:
            local_ip = '127.0.0.1'
        finally:
            s.close()

    # Ищем сертификат для выбранного IP
    cert = os.path.join(BASE_DIR, f'{local_ip}+2.pem')
    key = os.path.join(BASE_DIR, f'{local_ip}+2-key.pem')
    if os.path.exists(cert) and os.path.exists(key):
        ssl_ctx = (cert, key)
        print(f'🔒 HTTPS: https://{local_ip}:5000')
    else:
        ssl_ctx = None
        print(f'⚠️  HTTP:  http://{local_ip}:5000 (сканер не заработает без HTTPS — запусти setup-https.bat)')

    app.run(host='0.0.0.0', port=5000, ssl_context=ssl_ctx)
