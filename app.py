# -*- coding: utf-8 -*-
"""
Тонер-фарм — учёт тонеров/картриджей для IT-отдела.
Локальный Flask-сервер для внутренней сети (пилот без авторизации).
"""
import json
import os
import shutil
import sqlite3
from datetime import datetime, date, timedelta
from io import BytesIO

import qrcode
from flask import (Flask, g, jsonify, render_template, request, send_file,
                   send_from_directory, url_for)
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATABASE = os.path.join(BASE_DIR, 'database.db')
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
    ip_address TEXT,                    -- v2: опрос SNMP по ip_address (уровень тонера)
    toner_bk_id INTEGER,
    toner_c_id INTEGER,
    toner_m_id INTEGER,
    toner_y_id INTEGER
);
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    toner_id INTEGER,
    printer_id INTEGER,
    type TEXT,                          -- install / return / auto_depleted
    old_toner_id INTEGER,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    user_name TEXT
);
CREATE TABLE IF NOT EXISTS floor_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    image_path TEXT
);
"""

# Соответствие цвета картриджа колонке-слоту принтера
SLOT_COLUMN = {'Black': 'toner_bk_id', 'Cyan': 'toner_c_id',
               'Magenta': 'toner_m_id', 'Yellow': 'toner_y_id'}


def init_db():
    """Создание схемы и сид демо-данных, если таблицы пустые."""
    db = sqlite3.connect(DATABASE)
    db.executescript(SCHEMA)
    cur = db.execute('SELECT COUNT(*) FROM printers')
    if cur.fetchone()[0] == 0:
        seed_db(db)
    db.commit()
    db.close()


def seed_db(db):
    """Демо-данные для пилота."""
    db.executemany(
        'INSERT INTO printers (name, model, type, slots_count, x, y) VALUES (?,?,?,?,?,?)',
        [
            ('Бухгалтерия', 'HP Color LaserJet M452', 'color', 4, 20.0, 30.0),
            ('HR', 'HP LaserJet P1102', 'mono', 1, 55.0, 60.0),
            ('Отдел продаж', 'HP Color LaserJet M452', 'color', 4, 80.0, 25.0),
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
    db.execute(
        'INSERT INTO floor_plans (name, image_path) VALUES (?,?)',
        ('Этаж 1 (план-заглушка)', 'static/floor_plan.png'))


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
    """Цвет статуса принтера: green/yellow/red/grey."""
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
    toner = db.execute(
        "SELECT * FROM toners WHERE ean_13 = ? AND status != 'depleted' ORDER BY id DESC LIMIT 1",
        (ean,)).fetchone()
    resp = {'found': True, 'barcode': bc, 'toner': None, 'printer': None}
    if toner:
        resp['toner'] = toner_with_info(db, toner)
        if toner['status'] == 'installed' and toner['current_printer_id']:
            p = db.execute('SELECT * FROM printers WHERE id = ?',
                           (toner['current_printer_id'],)).fetchone()
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
    """Обучение нового EAN: запись в barcode_map + тонер на склад."""
    data = request.get_json(force=True)
    ean = (data.get('ean_13') or '').strip()
    if not ean:
        return jsonify({'error': 'ean_13 обязателен'}), 400
    compat = data.get('compatible_printers') or []
    if isinstance(compat, str):
        compat = [compat]
    db = get_db()
    db.execute(
        'INSERT OR REPLACE INTO barcode_map (ean_13, model_name, color, compatible_printers, page_yield) VALUES (?,?,?,?,?)',
        (ean, data.get('model_name'), data.get('color'),
         json.dumps(compat, ensure_ascii=False), data.get('page_yield')))
    db.execute("INSERT INTO toners (ean_13, status) VALUES (?, 'stock')", (ean,))
    db.commit()
    return jsonify({'ok': True, 'barcode': get_barcode(db, ean)})


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
    rows = db.execute('SELECT * FROM printers ORDER BY id').fetchall()
    return jsonify([serialize_printer(db, p) for p in rows])


@app.route('/api/printers', methods=['POST'])
def api_printers_create():
    data = request.get_json(force=True)
    ptype = data.get('type') or 'mono'
    db = get_db()
    cur = db.execute(
        'INSERT INTO printers (name, model, type, slots_count, x, y, ip_address) VALUES (?,?,?,?,?,?,?)',
        (data.get('name'), data.get('model'), ptype,
         4 if ptype == 'color' else 1,
         float(data.get('x') or 50), float(data.get('y') or 50),
         data.get('ip_address')))
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


@app.route('/api/floor_plan', methods=['POST'])
def api_floor_plan_upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'Файл не выбран'}), 400
    ext = os.path.splitext(f.filename)[1].lower() or '.png'
    rel = f'static/floor_plan_{datetime.now().strftime("%Y%m%d%H%M%S")}{ext}'
    f.save(os.path.join(BASE_DIR, rel))
    db = get_db()
    db.execute('INSERT INTO floor_plans (name, image_path) VALUES (?,?)',
               (f.filename, rel))
    db.commit()
    return jsonify({'ok': True, 'image_path': url_for('static_from_root', path=rel[len('static/'):])})


# отдача файлов из static/ (в т.ч. загруженных планов)
@app.route('/static/<path:path>')
def static_from_root(path):
    return send_from_directory(STATIC_DIR, path)


@app.route('/api/floor_plan', methods=['GET'])
def api_floor_plan_active():
    db = get_db()
    row = db.execute('SELECT * FROM floor_plans ORDER BY id DESC LIMIT 1').fetchone()
    if not row:
        return jsonify({'image_url': url_for('static', filename='floor_plan.png')})
    rel = row['image_path']
    if rel.startswith('static/'):
        rel = rel[len('static/'):]
    return jsonify({'id': row['id'], 'name': row['name'],
                    'image_url': url_for('static_from_root', path=rel)})


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
               'auto_depleted': 'Авто-списание'}
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
    import socket
    # Авто-определение локального IP: сертификат ищется по текущему IP,
    # поэтому при смене сети достаточно один раз запустить setup-https.bat
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'
    finally:
        s.close()

    cert = os.path.join(BASE_DIR, f'{local_ip}+2.pem')
    key = os.path.join(BASE_DIR, f'{local_ip}+2-key.pem')
    if os.path.exists(cert) and os.path.exists(key):
        ssl_ctx = (cert, key)
        print(f'HTTPS: https://{local_ip}:5000')
    else:
        ssl_ctx = None
        print(f'HTTP: http://{local_ip}:5000 (сканер на телефоне не заработает без HTTPS — запусти setup-https.bat)')

    app.run(host='0.0.0.0', port=5000, ssl_context=ssl_ctx)
