"""Доменная авторизация через LDAP (по умолчанию AD farm.local).

Роли по AD-группам (членство проверяется при входе):
- TonerFarm-View — чтение (карта, склад, история, статистика)
- IT_support — то же + изменения данных и админка

Домен и имена групп переопределяются переменными окружения
TF_DOMAIN / TF_BASE_DN / TF_LDAP_PORT / TF_VIEW_GROUP / TF_EDIT_GROUP —
на других площадках код править не нужно.

Сессия — подписанная кука Flask на SESSION_DAYS дней; пароль не храним.
"""
import os
import secrets
import time
import socket
import threading

from flask import session

# Настройки авторизации — переопределяются переменными окружения,
# чтобы на других площадках не править код (в Docker — через environment
# в docker-compose.yml). BASE_DN строится из домена, но можно задать явно.
DOMAIN = os.environ.get('TF_DOMAIN', 'farm.local')
BASE_DN = os.environ.get(
    'TF_BASE_DN',
    ','.join('DC=' + p for p in DOMAIN.split('.')))  # farm.local → DC=farm,DC=local
LDAP_PORT = int(os.environ.get('TF_LDAP_PORT', '389'))  # простой LDAP без TLS
LDAP_TIMEOUT = 1                     # секунд на ответ LDAP
LDAP_CONNECT_TIMEOUT = 1             # TCP + DNS таймаут
VIEW_GROUP = os.environ.get('TF_VIEW_GROUP', 'TonerFarm-View')
EDIT_GROUP = os.environ.get('TF_EDIT_GROUP', 'IT_support')
SESSION_DAYS = 30

# Локальный запасной вход (когда домен недоступен — дома, в Docker без LDAP).
# Выключается пустым паролем. Задаётся переменными окружения
# TF_LOCAL_ADMIN / TF_LOCAL_ADMIN_PASSWORD.
LOCAL_ADMIN = os.environ.get('TF_LOCAL_ADMIN', 'admin')
LOCAL_ADMIN_PASSWORD = os.environ.get('TF_LOCAL_ADMIN_PASSWORD', '')


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


def _ad_reachable(host, port, timeout=1):
    """TCP-пинг в отдельном потоке. На Windows getaddrinfo игнорирует timeout."""
    result = [None]
    def _check():
        try:
            with socket.create_connection((host, port), timeout=timeout):
                result[0] = True
        except Exception:
            result[0] = False
    t = threading.Thread(target=_check)
    t.daemon = True
    t.start()
    t.join(timeout + 0.5)
    return result[0] if result[0] is not None else False


def _groups_of(conn, username):
    """memberOf пользователя → set имён групп (CN). Пустой set = нет доступа.

    Ищем и по sAMAccountName (abramovv), и по userPrincipalName
    (Vladimir.Abramov@geropharm.com) — пользователь мог ввести любое.
    """
    import ldap3
    safe = ldap3.utils.conv.escape_filter_chars(username)
    conn.search(BASE_DN,
                f'(|(sAMAccountName={safe})(userPrincipalName={safe}))',
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
    """Проверка логина/пароля через LDAP. Возвращает (роль, canonical_name).

    Принимает любой формат: sAMAccountName (abramovv), FARM\\abramovv,
    UPN (Vladimir.Abramov@geropharm.com). None — при неверном пароле,
    недоступном AD или отсутствии членства в группах.

    До LDAP проверяется локальный запасной вход (TF_LOCAL_ADMIN /
    TF_LOCAL_ADMIN_PASSWORD) — для сетей, где домен недоступен.
    """
    username = (username or '').strip()
    if not username or not password:
        return None
    if '\\' in username:
        username = username.split('\\')[-1]
    # локальный запасной вход: активен, только если задан пароль
    if LOCAL_ADMIN_PASSWORD:
        login_part = username.split('@')[0].lower()
        if (login_part == LOCAL_ADMIN.lower()
                and secrets.compare_digest(password, LOCAL_ADMIN_PASSWORD)):
            return 'edit', LOCAL_ADMIN
    # если AD не отвечает по TCP за 1 сек — сразу отлуп, не ждём ldap3
    if not _ad_reachable(DOMAIN, LDAP_PORT, timeout=LDAP_CONNECT_TIMEOUT):
        return None
    bind_user = username if '@' in username else f'{username}@{DOMAIN}'
    import ldap3
    server = ldap3.Server(DOMAIN, port=LDAP_PORT, get_info=ldap3.NONE,
                          connect_timeout=LDAP_CONNECT_TIMEOUT)
    try:
        conn = ldap3.Connection(server, user=bind_user, password=password,
                                authentication=ldap3.SIMPLE, auto_bind=True,
                                receive_timeout=LDAP_TIMEOUT)
    except Exception:
        return None
    try:
        role = _role_of(_groups_of(conn, username))
        if not role:
            return None
        # canonical name для подписи операций: sAMAccountName, а не как ввели
        canonical = username.split('@')[0].lower()
        return role, canonical
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