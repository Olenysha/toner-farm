# -*- coding: utf-8 -*-
"""
Проверка SNMP + Web принтеров.
Диагностическая утилита; не используется основным приложением.
"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from snmp_monitor import snmp_get, snmp_walk
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== ПРИНТЕРЫ ГЕРОФАРМ ====================
PRINTERS = [
    ('10.7.150.201', 'HP Color LaserJet M477fdn (Коворкинг)'),
    ('10.7.200.87',  'HP Color LaserJet MFP M277dw (Медпредставители)'),
    ('10.7.150.242', 'HP Color LaserJet MFP M477fdn (Кузьмин)'),
    ('10.7.150.239', 'HP Color LaserJet Pro MFP M479fdn (Секретарь ГД)'),
    ('10.7.150.91',  'HP LaserJet 200 color M251n (Buh этикетки)'),
    ('10.7.150.235', 'Kyocera ECOSYS MA4000cix (Отдел Кадрового администрирования)'),
    ('10.7.150.244', 'Kyocera ECOSYS MA4000cix (Отдел по расчету ЗП)'),
    ('10.7.150.243', 'Kyocera ECOSYS MA4000cix (Ресепшен)'),
    ('10.7.150.246', 'Kyocera ECOSYS MA4000cix (Экономическая безопасность)'),
    ('10.7.150.237', 'Kyocera ECOSYS P3155dn BUH'),
    ('10.7.150.236', 'Kyocera ECOSYS P3155dn SALE'),
    ('10.7.150.231', 'Kyocera TASKalfa 3252ci (SPB_HR_LOG)'),
    ('10.7.150.232', 'Kyocera TASKalfa 3252ci (SPB_LAW_SALE)'),
    ('10.7.150.233', 'Kyocera TASKalfa 3253ci (SPB_FIN)'),
    ('10.7.150.241', 'XEROX WorkCentre 7220 (зеленый)'),
]
# ==========================================================

COMMUNITY = 'public'
TIMEOUT = 3


def check_web(ip):
    """Проверяем, открывается ли веб-интерфейс."""
    urls = [f'https://{ip}', f'http://{ip}']
    for url in urls:
        try:
            r = requests.get(url, timeout=5, verify=False)
            if r.status_code in (200, 401):
                return f'OK ({url}, status {r.status_code})'
        except Exception:
            pass
    return 'НЕДОСТУПЕН'


def check_printer(ip, name):
    print(f"\n{'='*70}")
    print(f"  {name}")
    print(f"  IP: {ip}")
    print('='*70)

    # 1. Web
    print(f"\n  [WEB]  {check_web(ip)}")

    # 2. SNMP базовая
    sys_name = snmp_get(ip, '1.3.6.1.2.1.1.5.0')
    sys_descr = snmp_get(ip, '1.3.6.1.2.1.1.1.0')
    sys_status = snmp_get(ip, '1.3.6.1.2.1.25.3.2.1.5.1')

    if sys_name is None and sys_descr is None:
        print("  [SNMP] НЕТ ОТВЕТА — SNMP выключен, неправильный community, или порт 161 закрыт")
        print("         Проверьте в настройках принтера: Network -> SNMP -> Enable")
        return

    print(f"  [SNMP] sysName  : {sys_name or '---'}")
    print(f"         sysDescr : {(sys_descr or '')[:100]}...")
    status_map = {'1': 'Running', '2': 'Warning', '3': 'Testing', '4': 'Down', '5': 'NotPresent'}
    print(f"         status   : {status_map.get(sys_status, sys_status or '---')}")

    # 3. Уровни тонера
    print("\n  --- Уровни тонера ---")
    levels = snmp_walk(ip, '1.3.6.1.2.1.43.11.1.1.9')
    descriptions = snmp_walk(ip, '1.3.6.1.2.1.43.11.1.1.6')
    maxcaps = snmp_walk(ip, '1.3.6.1.2.1.43.11.1.1.8')

    if not levels:
        print("  Стандартные OID не отвечают. Пробуем vendor-specific...")
        check_vendor_specific(ip, name)
    else:
        desc_map = {}
        for oid, val in descriptions:
            idx = oid.split('.')[-1]
            desc_map[idx] = val
        max_map = {}
        for oid, val in maxcaps:
            idx = oid.split('.')[-1]
            max_map[idx] = val

        for oid, val in levels:
            idx = oid.split('.')[-1]
            desc = desc_map.get(idx, f'Картридж #{idx}')
            maxcap = max_map.get(idx, '0')
            try:
                level = int(val)
                maxv = int(maxcap) if maxcap and int(maxcap) > 0 else 100
                if maxv > 0 and level >= 0:
                    pct = round(level / maxv * 100)
                    bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
                    print(f"  {desc:35s} {bar} {level}/{maxv} ({pct}%)")
                else:
                    print(f"  {desc:35s} {level} (абс. значение)")
            except ValueError:
                print(f"  {desc:35s} {val}")

    # 4. Счетчик страниц
    print("\n  --- Счетчик страниц ---")
    pages = snmp_get(ip, '1.3.6.1.2.1.43.10.2.1.4.1.1')
    if pages:
        print(f"  Total pages: {pages}")
    else:
        print("  --- (не отдает)")

    # 5. Алерты
    print("\n  --- Алерты ---")
    alerts = snmp_walk(ip, '1.3.6.1.2.1.43.18.1.1.7')
    if alerts:
        for _, msg in alerts:
            print(f"  ⚠️  {msg}")
    else:
        print("  Нет активных алертов")


def check_vendor_specific(ip, name):
    found = False

    if 'Kyocera' in name:
        kyocera_oids = [
            ('1.3.6.1.4.1.1347.42.2.1.1.1.6.1.1', 'Kyocera Black %'),
            ('1.3.6.1.4.1.1347.42.2.1.1.1.6.2.1', 'Kyocera Cyan %'),
            ('1.3.6.1.4.1.1347.42.2.1.1.1.6.3.1', 'Kyocera Magenta %'),
            ('1.3.6.1.4.1.1347.42.2.1.1.1.6.4.1', 'Kyocera Yellow %'),
        ]
        for oid, label in kyocera_oids:
            val = snmp_get(ip, oid)
            if val and 'ERROR' not in val:
                print(f"  {label}: {val}%")
                found = True

    if 'Xerox' in name or 'XEROX' in name:
        xerox = snmp_walk(ip, '1.3.6.1.4.1.253.8.51.2.2.1.1')
        if xerox:
            print(f"  Xerox supply data: {len(xerox)} записей")
            found = True

    if 'Brother' in name:
        val = snmp_get(ip, '1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.1.0')
        if val:
            print(f"  Brother toner status: {val}")
            found = True

    if 'HP' in name:
        hp = snmp_walk(ip, '1.3.6.1.4.1.11.2.3.9.4.2.1.4.1')
        if hp:
            print(f"  HP supply data: {len(hp)} записей")
            found = True

    if not found:
        print("  Vendor-specific OID тоже не отвечают.")
        print("  Возможно, SNMP включен, но community не 'public' или доступ ограничен.")


if __name__ == '__main__':
    print("=" * 70)
    print("  Проверка SNMP + Web принтеров Герофарм")
    print("=" * 70)

    for ip, name in PRINTERS:
        check_printer(ip, name)

    print("\n" + "=" * 70)
    print("Готово. Скопируйте вывод и пришлите мне.")