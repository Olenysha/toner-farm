# -*- coding: utf-8 -*-
"""Тонер-фарм — слой данных: пути, схема SQLite, миграции, сид, бэкапы,
общие хелперы сериализации. Не содержит Flask-роутов."""
import json
import os
import shutil
import sqlite3
from datetime import datetime, date, timedelta

from flask import g

from alerts_data import ALERT_CODES_SEED, ALERT_DB_SCHEMA

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Данные (БД, бэкапы) можно вынести в отдельный каталог — в Docker это volume
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR)
DATABASE = os.path.join(DATA_DIR, 'database.db')
ALERT_DATABASE = os.path.join(DATA_DIR, 'alerts.db')  # справочник SNMP-алертов
BACKUP_DIR = os.path.join(DATA_DIR, 'backups')
STATIC_DIR = os.path.join(BASE_DIR, 'static')
QR_DIR = os.path.join(STATIC_DIR, 'qr_codes')
FLOOR_PLAN_PATH = os.path.join(STATIC_DIR, 'floor_plan.png')

# Сколько дней тонер считается «стареющим» (жёлтый статус на карте)
AGING_DAYS = 60


def now_str():
    """Текущее локальное время компьютера как 'YYYY-MM-DD HH:MM:SS'.

    Все таймстемпы пишем локальным временем машины (а не UTC,
    как SQLite CURRENT_TIMESTAMP) — корректно в любой таймзоне.
    """
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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
    quantity INTEGER DEFAULT 1,
    enterprise_id INTEGER              -- склад какого предприятия (barcode_map при этом общий)
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
    timestamp TIMESTAMP DEFAULT (datetime('now','localtime')),  -- локальное время машины
    user_name TEXT
);
CREATE TABLE IF NOT EXISTS enterprises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS floor_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    image_path TEXT,
    enterprise_id INTEGER               -- предприятие (enterprises.id), которому принадлежит этаж
);
CREATE TABLE IF NOT EXISTS snmp_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT (datetime('now','localtime')),  -- локальное время машины
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


# ---------------------------------------------------------------- Соединение

def get_db():
    """Соединение с SQLite через flask.g."""
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


def close_db(exc=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------- Инициализация

def init_db():
    """Создание схемы, миграции и сид демо-данных, если таблицы пустые."""
    os.makedirs(DATA_DIR, exist_ok=True)
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


def load_ip_map():
    """Карта «имя принтера → IP» из printer_ips.json (для миграции)."""
    path = os.path.join(BASE_DIR, 'printer_ips.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith('_')}


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
    # проставляем IP принтерам, которых однозначно находим в сети (printer_ips.json)
    for name, ip in load_ip_map().items():
        db.execute('UPDATE printers SET ip_address=? WHERE name=? AND ip_address IS NULL', (ip, name))
    migrate_enterprises(db)


def migrate_enterprises(db):
    """Предприятия: колонка floor_plans.enterprise_id + предприятие по умолчанию.

    Все существующие этажи привязываются к первому предприятию,
    данные не удаляются.
    """
    cols = [r[1] for r in db.execute('PRAGMA table_info(floor_plans)')]
    if 'enterprise_id' not in cols:
        db.execute('ALTER TABLE floor_plans ADD COLUMN enterprise_id INTEGER')
    ent_id = db.execute('SELECT MIN(id) FROM enterprises').fetchone()[0]
    if ent_id is None:
        cur = db.execute("INSERT INTO enterprises (name) VALUES ('Герофарм')")
        ent_id = cur.lastrowid
    db.execute('UPDATE floor_plans SET enterprise_id=? WHERE enterprise_id IS NULL', (ent_id,))
    # склады: toners.enterprise_id (существующие тонеры → первое предприятие)
    tcols = [r[1] for r in db.execute('PRAGMA table_info(toners)')]
    if 'enterprise_id' not in tcols:
        db.execute('ALTER TABLE toners ADD COLUMN enterprise_id INTEGER')
    db.execute('UPDATE toners SET enterprise_id=? WHERE enterprise_id IS NULL', (ent_id,))
    migrate_timestamps_local(db)


def migrate_timestamps_local(db):
    """Разово переводим старые UTC-таймстемпы в локальное время.

    Раньше operations/snmp_readings писались через DEFAULT CURRENT_TIMESTAMP
    (UTC). Модификатор SQLite 'localtime' конвертирует UTC → локальное время
    этой машины, поэтому сработает правильно в любой таймзоне.
    Однократность — через PRAGMA user_version (0 → 1).
    toners.installed_at не трогаем: он всегда писался локальным временем.
    """
    if db.execute('PRAGMA user_version').fetchone()[0] != 0:
        return
    db.execute("UPDATE operations SET timestamp = datetime(timestamp, 'localtime')")
    db.execute("UPDATE snmp_readings SET timestamp = datetime(timestamp, 'localtime')")
    db.execute('PRAGMA user_version = 1')


def seed_db(db):
    """Демо-данные для пилота: реальные принтеры Герофарм."""
    floor_id = db.execute('SELECT MIN(id) FROM floor_plans').fetchone()[0]
    ent_id = db.execute('SELECT MIN(id) FROM enterprises').fetchone()[0]
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
        "INSERT INTO toners (ean_13, status, enterprise_id) VALUES (?, 'stock', ?)",
        [('0886111244457', ent_id), ('0886111244457', ent_id), ('0886111244464', ent_id)])


# ------------------------------------------------------------------ alerts.db

def ensure_alerts_db():
    """Создаём alerts.db и сидим коды, если таблица пустая."""
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(ALERT_DATABASE)
    db.executescript(ALERT_DB_SCHEMA)
    if db.execute('SELECT COUNT(*) FROM alert_codes').fetchone()[0] == 0:
        db.executemany(
            'INSERT OR IGNORE INTO alert_codes (vendor, code, name, title_ru, hint_ru) VALUES (?,?,?,?,?)',
            ALERT_CODES_SEED)
        db.commit()
    db.close()


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
