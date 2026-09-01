#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SNMP-агент для удалённой сети (Android/Termux, Raspberry Pi и т.п.).

Опрашивает принтер по SNMP и отправляет результат на сервер Тонер-Фарм
через HTTP POST на /api/agents/snmp_report.

Настройки задаются переменными окружения (удобно для Termux):
  export SERVER_URL="https://имя_ноутбука:5000/api/agents/snmp_report"
  export AGENT_TOKEN="токен_из_.env"
  export PRINTER_ID="123"
  export PRINTER_IP="10.0.0.50"
  export SNMP_COMMUNITY="public"
  export INTERVAL="600"

Запуск:
  python snmp_agent.py

Для Android через Termux:
  pkg install python
  pip install pysnmp==7.1.29 requests
  python snmp_agent.py
"""
import asyncio
import json
import os
import re
import sys
import time
import urllib3
from datetime import datetime

import requests
from pysnmp.hlapi.asyncio import (CommunityData, ContextData, ObjectIdentity,
                                  ObjectType, SnmpEngine, UdpTransportTarget,
                                  get_cmd, next_cmd)

# ---------- настройки ----------
SERVER_URL = os.environ.get('SERVER_URL', '').rstrip('/')
AGENT_TOKEN = os.environ.get('AGENT_TOKEN', '')
PRINTER_ID = os.environ.get('PRINTER_ID', '')
PRINTER_IP = os.environ.get('PRINTER_IP', '')
COMMUNITY = os.environ.get('SNMP_COMMUNITY', 'public')
SNMP_TIMEOUT = int(os.environ.get('SNMP_TIMEOUT', '5'))
INTERVAL = int(os.environ.get('INTERVAL', '600'))
VERIFY_SSL = os.environ.get('VERIFY_SSL', 'false').lower() in ('1', 'true', 'yes')

# на сервере самоподписанный сертификат — по умолчанию не проверяем
if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ---------- SNMP helpers ----------
async def _snmp_get(ip, oid):
    try:
        target = await UdpTransportTarget.create((ip, 161), timeout=SNMP_TIMEOUT, retries=1)
        err_ind, err_sts, err_idx, var_binds = await get_cmd(
            SnmpEngine(), CommunityData(COMMUNITY, mpModel=1),
            target, ContextData(), ObjectType(ObjectIdentity(oid)))
        if err_ind or err_sts or not var_binds:
            return None
        return str(var_binds[0][1])
    except Exception:
        return None


async def _snmp_walk(ip, base_oid):
    results = []
    try:
        target = await UdpTransportTarget.create((ip, 161), timeout=SNMP_TIMEOUT, retries=1)
        engine = SnmpEngine()
        auth = CommunityData(COMMUNITY, mpModel=1)
        current = ObjectType(ObjectIdentity(base_oid))
        for _ in range(500):
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


def _color_from_desc(desc):
    d = (desc or '').strip().lower()
    if not d:
        return None
    head = re.split(r'[;,]', d)[0].strip()
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
    m = re.search(r'([kcmy])$', head)
    if m:
        return {'k': 'black', 'c': 'cyan', 'm': 'magenta', 'y': 'yellow'}[m.group(1)]
    return 'black'


async def poll_printer(ip):
    sys_name = await _snmp_get(ip, '1.3.6.1.2.1.1.5.0')
    sys_descr = await _snmp_get(ip, '1.3.6.1.2.1.1.1.0')
    sys_status = await _snmp_get(ip, '1.3.6.1.2.1.25.3.2.1.5.1')

    status_map = {'1': 'Running', '2': 'Warning', '3': 'Testing',
                  '4': 'Down', '5': 'NotPresent'}
    status_text = status_map.get(sys_status, sys_status or 'Unknown')

    # если принтер вообще не отвечает — сервер сам сделает серую запись
    if sys_name is None and sys_descr is None:
        return None

    levels = await _snmp_walk(ip, '1.3.6.1.2.1.43.11.1.1.9')
    descriptions = await _snmp_walk(ip, '1.3.6.1.2.1.43.11.1.1.6')
    maxcaps = await _snmp_walk(ip, '1.3.6.1.2.1.43.11.1.1.8')

    black_level = cyan_level = magenta_level = yellow_level = None
    drums = []

    if levels:
        desc_map = {oid.split('.')[-1]: val.lower() for oid, val in descriptions}
        desc_orig = {oid.split('.')[-1]: val for oid, val in descriptions}
        max_map = {oid.split('.')[-1]: val for oid, val in maxcaps}
        for oid, val in levels:
            idx = oid.split('.')[-1]
            desc = desc_map.get(idx, '')
            color = _color_from_desc(desc)
            maxcap = max_map.get(idx, '0')
            try:
                level = int(val)
                maxv = int(maxcap) if maxcap and int(maxcap) > 0 else 100
                pct = round(level / maxv * 100) if maxv > 0 and level >= 0 else level
            except ValueError:
                continue
            if color is None:
                if 'drum' in desc:
                    drums.append({
                        'desc': re.split(r'[;,]', desc_orig.get(idx, desc))[0].strip(),
                        'pct': pct})
                continue
            if color == 'black':
                black_level = pct
            elif color == 'cyan':
                cyan_level = pct
            elif color == 'magenta':
                magenta_level = pct
            elif color == 'yellow':
                yellow_level = pct

    page_counter = await _snmp_get(ip, '1.3.6.1.2.1.43.10.2.1.4.1.1')
    try:
        page_counter = int(page_counter) if page_counter else None
    except ValueError:
        page_counter = None

    return {
        'black_level': black_level,
        'cyan_level': cyan_level,
        'magenta_level': magenta_level,
        'yellow_level': yellow_level,
        'page_counter': page_counter,
        'status_text': status_text,
        'alerts': [],  # расшифровка алертов остаётся на сервере
        'raw_data': {'sys_name': sys_name, 'sys_descr': sys_descr, 'drums': drums}
    }


def send_report(payload):
    if not SERVER_URL.endswith('/api/agents/snmp_report'):
        url = SERVER_URL + '/api/agents/snmp_report'
    else:
        url = SERVER_URL
    try:
        r = requests.post(
            url, json=payload, timeout=15,
            verify=VERIFY_SSL)
        return r.status_code, r.text
    except Exception as e:
        return None, str(e)


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def main():
    errors = []
    if not AGENT_TOKEN:
        errors.append('AGENT_TOKEN')
    if not PRINTER_IP:
        errors.append('PRINTER_IP')
    if not SERVER_URL:
        errors.append('SERVER_URL')
    if errors:
        print('Не заданы переменные:', ', '.join(errors))
        print('Пример:')
        print('export SERVER_URL=https://имя_ноутбука:5000/api/agents/snmp_report')
        print('export AGENT_TOKEN=токен_из_.env')
        print('export PRINTER_IP=10.0.0.50')
        print('export PRINTER_ID=123')
        sys.exit(1)

    print(f'[{now()}] Агент запущен')
    print(f'  принтер: {PRINTER_IP}')
    print(f'  сервер:  {SERVER_URL}')
    print(f'  интервал: {INTERVAL} сек')

    while True:
        try:
            data = asyncio.run(poll_printer(PRINTER_IP))
            if data is None:
                print(f'[{now()}] принтер не отвечает, пропускаю цикл')
                time.sleep(INTERVAL)
                continue

            payload = {
                'token': AGENT_TOKEN,
                'printer_id': int(PRINTER_ID) if PRINTER_ID else None,
                'ip_address': PRINTER_IP,
                **data
            }
            status, body = send_report(payload)
            print(f'[{now()}] сервер: HTTP {status} {body}')
        except Exception as e:
            print(f'[{now()}] ошибка: {e}')
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
