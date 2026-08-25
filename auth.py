# -*- coding: utf-8 -*-
"""Доменная авторизация через LDAP (по умолчанию AD farm.local).

Роли по AD-группам (членство проверяется при входе):
- TonerFarm-View — чтение (карта, склад, история, статистика)
- TonerFarm-Edit — то же + изменения данных и админка

Домен и имена групп переопределяются переменными окружения
TF_DOMAIN / TF_BASE_DN / TF_LDAP_PORT / TF_VIEW_GROUP / TF_EDIT_GROUP —
на других площадках код править не нужно.

Сессия — подписанная кука Flask на SESSION_DAYS дней; пароль не храним.
"""
import os
import secrets
import time

from flask import session

# Настройки авторизации — переопределяются переменными окружения,
# чтобы на других площадках не править код (в Docker — через environment
# в docker-compose.yml). BASE_DN строится из домена, но можно задать явно.
DOMAIN = os.environ.get('TF_DOMAIN', 'farm.local')
BASE_DN = os.environ.get(
    'TF_BASE_DN',
    ','.join('DC=' + p for p in DOMAIN.split('.')))  # farm.local → DC=farm,DC=local
LDAP_PORT = int(os.environ.get('TF_LDAP_PORT', '389'))  # простой LDAP без TLS
LDAP_TIMEOUT = 5                     # секунд на подключение/ответ контроллера
VIEW_GROUP = os.environ.get('TF_VIEW_GROUP', 'TonerFarm-View')
EDIT_GROUP = os.environ.get('TF_EDIT_GROUP', 'TonerFarm-Edit')
SESSION_DAYS = 30


def load_or_create_secret(data_dir):
    """Ключ подписи сессий: читаем из data_dir/secret_key или создаём."""
    path = os.path.join(data_dir, 'secret_key')
    if os.path.exists(path):
        with open(path, 'r', encoding='ascii') as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    key = secrets.token_hex(32)
    with open(path, 'w', encoding='ascii') as f:
        f.write(key)
    return key


def _groups_of(conn, username):
    """memberOf пользователя → set имён групп (CN). Пустой set = нет доступа."""
    import ldap3
    safe = ldap3.utils.conv.escape_filter_chars(username)
    conn.search(BASE_DN, f'(sAMAccountName={safe})',
                search_scope=ldap3.SUBTREE, attributes=['memberOf'],
                time_limit=LDAP_TIMEOUT)
    if not conn.entries:
        return set()
    groups = set()
    for dn in conn.entries[0]['memberOf'].values:
        # DN вида 'CN=TonerFarm-View,OU=...,DC=farm,DC=local' → берём CN
        for part in str(dn).split(','):
            if part.upper().startswith('CN='):
                groups.add(part[3:])
                break
    return groups


def _role_of(groups):
    if EDIT_GROUP in groups:
        return 'edit'
    if VIEW_GROUP in groups:
        return 'view'
    return None


def try_login(username, password):
    """Проверка логина/пароля через LDAP. Возвращает роль или None.

    None — и при неверном пароле, и при недоступном AD, и когда
    пользователь не входит ни в одну из групп TonerFarm-*.
    """
    username = (username or '').strip().split('\\')[-1].split('@')[0]
    if not username or not password:
        return None
    import ldap3
    server = ldap3.Server(DOMAIN, port=LDAP_PORT, get_info=ldap3.NONE,
                          connect_timeout=LDAP_TIMEOUT)
    try:
        conn = ldap3.Connection(server, user=f'{username}@{DOMAIN}',
                                password=password,
                                authentication=ldap3.SIMPLE, auto_bind=True,
                                receive_timeout=LDAP_TIMEOUT)
    except Exception:
        return None
    try:
        return _role_of(_groups_of(conn, username))
    except Exception:
        return None
    finally:
        conn.unbind()


def current_user():
    """Текущий пользователь из сессии: {'name': ..., 'role': ...} или None."""
    u = session.get('user')
    if not u or 'name' not in u or 'role' not in u:
        return None
    return u


def login_session(username, role):
    session.permanent = True
    session['user'] = {'name': username, 'role': role,
                       'since': int(time.time())}
