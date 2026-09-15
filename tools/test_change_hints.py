# -*- coding: utf-8 -*-
"""Смоук-тест детекции замены тонера и эндпоинтов change_hints."""
import os
import sqlite3
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix='tf_test_')
os.environ['DATA_DIR'] = tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
from db import init_db, now_str

init_db()

# --- готовим принтер и склад ---
conn = sqlite3.connect(db.DATABASE)
conn.row_factory = sqlite3.Row
p = conn.execute("SELECT id, type FROM printers LIMIT 1").fetchone()
pid = p['id']
print('принтер:', dict(p))

# EAN для модели этого принтера: добавляем свою запись в справочник
model = conn.execute('SELECT model FROM printers WHERE id=?', (pid,)).fetchone()['model']
import json as _json
conn.execute(
    'INSERT OR REPLACE INTO barcode_map (ean_13, model_name, color, compatible_printers) '
    "VALUES ('4600012345678', 'Test Toner', 'Black', ?)",
    (_json.dumps([model]),))
conn.commit()
bc = conn.execute("SELECT * FROM barcode_map WHERE ean_13='4600012345678'").fetchone()
print('штрихкод:', dict(bc) if bc else None)
if p['type'] == 'color' and bc and bc['color'] != 'Black':
    # для цветного принтера берём чёрный слот для простоты
    pass

# складская позиция
cur = conn.execute(
    "INSERT INTO toners (ean_13, status, enterprise_id) VALUES (?, 'stock', NULL)",
    (bc['ean_13'],))
toner_id = cur.lastrowid
conn.commit()

# --- эмуляция двух SNMP-снимков: 1% -> 95% ---
from snmp_monitor import _detect_toner_changes

col = 'black_level' if bc['color'] == 'Black' else 'cyan_level'
conn.execute(
    'INSERT INTO snmp_readings (printer_id, timestamp, black_level, cyan_level, magenta_level, yellow_level) '
    'VALUES (?,?,?,?,?,?)', (pid, now_str(), 1, 1, 1, 1))
conn.commit()
rid = conn.execute('SELECT MAX(id) FROM snmp_readings').fetchone()[0]
levels = {'black_level': 1, 'cyan_level': 1, 'magenta_level': 1, 'yellow_level': 1}
_detect_toner_changes(conn, pid, rid, levels)  # 1 -> 1, скачка нет
conn.commit()
hints = conn.execute('SELECT COUNT(*) FROM toner_change_hints').fetchone()[0]
assert hints == 0, f'ложное срабатывание: {hints}'

conn.execute(
    'INSERT INTO snmp_readings (printer_id, timestamp, black_level, cyan_level, magenta_level, yellow_level) '
    'VALUES (?,?,?,?,?,?)', (pid, now_str(), 95, 95, 95, 95))
conn.commit()
rid = conn.execute('SELECT MAX(id) FROM snmp_readings').fetchone()[0]
levels = {'black_level': 95, 'cyan_level': 95, 'magenta_level': 95, 'yellow_level': 95}
_detect_toner_changes(conn, pid, rid, levels)  # 1 -> 95, скачок
conn.commit()
rows = conn.execute('SELECT * FROM toner_change_hints').fetchall()
print('подсказки:', [dict(r) for r in rows])
# цветной принтер даст 4 подсказки, моно — 1
assert len(rows) >= 1
assert all(r['status'] == 'pending' for r in rows)

# повторный вызов с теми же данными не плодит дубликаты
_detect_toner_changes(conn, pid, rid, levels)
conn.commit()
assert conn.execute('SELECT COUNT(*) FROM toner_change_hints').fetchone()[0] == len(rows)
conn.close()

# --- эндпоинты через тест-клиент ---
import app as application
c = application.app.test_client()

with c.session_transaction() as s:
    s.permanent = True
    s['user'] = {'name': 'tester', 'role': 'edit', 'since': 0}

r = c.get('/api/change_hints')
assert r.status_code == 200, r.data
hints = r.get_json()
print('GET /api/change_hints:', len(hints))
assert len(hints) == len(rows)

h = hints[0]
r = c.post(f"/api/change_hints/{h['id']}/dismiss")
assert r.status_code == 200, r.data

h2 = hints[1] if len(hints) > 1 else None
if h2:
    r = c.post(f"/api/change_hints/{h2['id']}/confirm", json={'toner_id': toner_id})
    assert r.status_code == 200, r.data
    # тонер стал installed
    conn = sqlite3.connect(db.DATABASE)
    st = conn.execute('SELECT status, current_printer_id FROM toners WHERE id=?',
                      (toner_id,)).fetchone()
    conn.close()
    assert st[0] == 'installed' and st[1] == pid, st
    print('confirm: тонер установлен в принтер', st)

# view-роль получает 403
with c.session_transaction() as s:
    s.permanent = True
    s['user'] = {'name': 'viewer', 'role': 'view', 'since': 0}
r = c.get('/api/change_hints')
assert r.status_code == 403, r.status_code

print('SMOKE OK')
