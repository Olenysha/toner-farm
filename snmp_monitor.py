# -*- coding: utf-8 -*-
"""Тонер-фарм — SNMP-мониторинг принтеров (pysnmp 7, asyncio API).

Фоновый опрос всех принтеров с ip_address раз в SNMP_INTERVAL секунд;
результаты пишутся в таблицу snmp_readings. Расшифровка алертов — через
справочник alerts.db (данные в alerts_data.py).
"""
import asyncio
import json
import re
import sqlite3
import threading
import time

from pysnmp.hlapi.asyncio import (SnmpEngine, CommunityData,
                                  UdpTransportTarget, ContextData,
                                  ObjectType, ObjectIdentity,
                                  get_cmd, next_cmd)

from db import DATABASE, ALERT_DATABASE

COMMUNITY = 'public'
SNMP_TIMEOUT = 3          # секунд на один SNMP-запрос
SNMP_INTERVAL = 600       # секунд между фоновыми опросами принтеров
WALK_MAX_STEPS = 500      # страховка от бесконечного walk

# prtAlertSeverityLevel: other(1), critical(3), serious(4), warning(5)
SEVERITY_ICON = {'3': '🔴', '4': '🟠', '5': '🟡', '1': 'ℹ️'}


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


# ------------------------------------------------------------------ SNMP I/O

async def _snmp_get(ip, oid):
    """Одиночный OID. Возвращает строку или None."""
    try:
        target = await UdpTransportTarget.create(
            (ip, 161), timeout=SNMP_TIMEOUT, retries=1)
        err_ind, err_sts, err_idx, var_binds = await get_cmd(
            SnmpEngine(),
            CommunityData(COMMUNITY, mpModel=1),
            target,
            ContextData(),
            ObjectType(ObjectIdentity(oid)))
        if err_ind or err_sts or not var_binds:
            return None
        return str(var_binds[0][1])
    except Exception:
        return None


async def _snmp_walk(ip, base_oid):
    """Walk по ветке. Возвращает список (oid, value)."""
    results = []
    try:
        target = await UdpTransportTarget.create(
            (ip, 161), timeout=SNMP_TIMEOUT, retries=1)
        engine = SnmpEngine()
        auth = CommunityData(COMMUNITY, mpModel=1)
        current = ObjectType(ObjectIdentity(base_oid))
        for _ in range(WALK_MAX_STEPS):
            err_ind, err_sts, err_idx, var_binds = await next_cmd(
                engine, auth, target, ContextData(), current,
                lexicographicMode=False)
            if err_ind or err_sts or not var_binds:
                break
            oid_str = str(var_binds[-1][0])
            if not oid_str.startswith(base_oid):
                break
            for oid, val in var_binds:
                results.append((str(oid), str(val)))
            current = ObjectType(ObjectIdentity(oid_str))
    except Exception:
        pass
    return results


def snmp_get(ip, oid):
    """Синхронная обёртка над _snmp_get (для check_printers.py)."""
    return asyncio.run(_snmp_get(ip, oid))


def snmp_walk(ip, base_oid):
    """Синхронная обёртка над _snmp_walk (для check_printers.py)."""
    return asyncio.run(_snmp_walk(ip, base_oid))


# ------------------------------------------------------------------ Опрос

async def _poll_printer(db, pid, ip, model, name):
    """Опрос одного принтера и запись результата в snmp_readings."""
    sys_name = await _snmp_get(ip, '1.3.6.1.2.1.1.5.0')
    sys_descr = await _snmp_get(ip, '1.3.6.1.2.1.1.1.0')
    sys_status = await _snmp_get(ip, '1.3.6.1.2.1.25.3.2.1.5.1')

    if sys_name is None and sys_descr is None:
        # принтер не отвечает (SNMP выключен / недоступен) — остаётся серым
        return

    status_map = {'1': 'Running', '2': 'Warning', '3': 'Testing',
                  '4': 'Down', '5': 'NotPresent'}
    status_text = status_map.get(sys_status, sys_status or 'Unknown')

    # уровни тонера (стандартный Printer-MIB)
    levels = await _snmp_walk(ip, '1.3.6.1.2.1.43.11.1.1.9')
    descriptions = await _snmp_walk(ip, '1.3.6.1.2.1.43.11.1.1.6')
    maxcaps = await _snmp_walk(ip, '1.3.6.1.2.1.43.11.1.1.8')

    black_level = cyan_level = magenta_level = yellow_level = None

    if levels:
        desc_map = {oid.split('.')[-1]: val.lower() for oid, val in descriptions}
        max_map = {oid.split('.')[-1]: val for oid, val in maxcaps}

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
    page_counter = await _snmp_get(ip, '1.3.6.1.2.1.43.10.2.1.4.1.1')
    try:
        page_counter = int(page_counter) if page_counter else None
    except ValueError:
        page_counter = None

    # алерты: код (.7) + важность (.2) + описание устройства (.8)
    adb = sqlite3.connect(ALERT_DATABASE)
    try:
        vendor = detect_vendor(model, name, sys_descr)
        alerts = decode_alerts(
            adb, vendor,
            await _snmp_walk(ip, '1.3.6.1.2.1.43.18.1.1.7'),
            await _snmp_walk(ip, '1.3.6.1.2.1.43.18.1.1.2'),
            await _snmp_walk(ip, '1.3.6.1.2.1.43.18.1.1.8'))
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


async def _poll_all():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    printers = db.execute(
        'SELECT * FROM printers WHERE ip_address IS NOT NULL').fetchall()
    for p in printers:
        try:
            await _poll_printer(db, p['id'], p['ip_address'], p['model'], p['name'])
        except Exception:
            # любая ошибка по одному принтеру не должна ронять опрос остальных
            continue
    db.close()


def poll_all_printers():
    """Опрос всех принтеров с IP и запись в snmp_readings."""
    asyncio.run(_poll_all())


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
