# -*- coding: utf-8 -*-
"""Диагностика LDAP-логина Тонер-фарм. Ничего не меняет, только читает AD.

Запуск:  .venv\\Scripts\\python.exe test_ldap.py
Введите свой доменный логин и пароль — скрипт пройдёт те же шаги,
что делает auth.try_login, и покажет, где именно отказ.
"""
import getpass

import ldap3

import auth

print(f'Настройки: домен={auth.DOMAIN}, база={auth.BASE_DN}, порт={auth.LDAP_PORT}')
print(f'Группы: view={auth.VIEW_GROUP!r}, edit={auth.EDIT_GROUP!r}')
print()

username = input('Логин (abramovv / FARM\\abramovv / почта): ').strip()
password = getpass.getpass('Пароль (не отображается): ')

login = username.split('\\')[-1]
bind_user = login if '@' in login else f'{login}@{auth.DOMAIN}'
print(f'\n1. Бинд как {bind_user!r} ...')
server = ldap3.Server(auth.DOMAIN, port=auth.LDAP_PORT, get_info=ldap3.NONE,
                      connect_timeout=auth.LDAP_TIMEOUT)
try:
    conn = ldap3.Connection(server, user=bind_user, password=password,
                            authentication=ldap3.SIMPLE, auto_bind=True,
                            receive_timeout=auth.LDAP_TIMEOUT)
    print('   OK — логин/пароль верные, домен доступен')
except Exception as e:
    print(f'   FAIL: {type(e).__name__}: {e}')
    print('   → Проблема на этом шаге: пароль, блокировка учётки или bind.')
    raise SystemExit(1)

print(f'\n2. Поиск пользователя и чтение memberOf ...')
try:
    groups = auth._groups_of(conn, login)
    conn.unbind()
except Exception as e:
    print(f'   FAIL: {type(e).__name__}: {e}')
    raise SystemExit(1)
print(f'   OK — групп найдено: {len(groups)}')
for g in sorted(groups):
    mark = '  <-- совпадение' if g in (auth.VIEW_GROUP, auth.EDIT_GROUP) else ''
    print(f'   - {g}{mark}')

print(f'\n3. Итог: try_login = {auth.try_login(username, password)!r}')
print('   (None = не пустит: неверный пароль, нет группы или домен недоступен)')
