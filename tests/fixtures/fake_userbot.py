"""Фальшивый помощник аккаунта Telegram: тот же протокол строк JSON, что у bender/userbot/helper.py, без сети и Telethon.

Сценарий — в переменной FAKE_USERBOT (JSON): authorized, me, password, chats, history, fail {метод: {code,message}}.
Каждый запрос повторяется событием _log (песочница не даёт помощнику писать файлы). Команда {"method":"_push","params":{"message":…}} имитирует входящее сообщение.
"""
import json
import os
import sys

CFG = json.loads(os.environ.get('FAKE_USERBOT') or '{}') or {'chats': [
    {'id': -1001, 'type': 'supergroup', 'title': 'Рабочая группа', 'username': 'workgroup', 'unread': 3},
    {'id': -1002, 'type': 'channel', 'title': 'Новости проекта', 'username': '', 'unread': 0},
    {'id': 55, 'type': 'user', 'title': 'Вася', 'username': 'vasya', 'unread': 0}]}
ME = CFG.get('me') or {'id': 777, 'name': 'Агент', 'username': 'agent_acc', 'bot': False}
state = {'authorized': False, 'next': 1000}


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def handle(method, p):
    fail = (CFG.get('fail') or {}).get(method)
    if fail:
        return None, fail
    if method == 'configure':
        state['authorized'] = bool(p.get('session')) and p.get('session') != 'revoked'
        if p.get('session') == 'revoked':
            return None, {'code': 'auth', 'message': 'Вход в аккаунт больше не действует. Войди заново.'}
        return ({'authorized': True, 'me': ME} if state['authorized'] else {'authorized': False}), None
    if method == 'login_start':
        return {'sent': True, 'via': 'app'}, None
    if method == 'login_code':
        if p.get('code') != '12345':
            return None, {'code': 'telegram', 'message': 'Код не подошёл.'}
        if CFG.get('password'):
            return {'authorized': False, 'password_needed': True}, None
        state['authorized'] = True
        return {'authorized': True, 'session': 'SESSION-STRING', 'me': ME}, None
    if method == 'login_password':
        if p.get('password') != CFG.get('password'):
            return None, {'code': 'telegram', 'message': 'Пароль двухэтапной проверки не подошёл.'}
        state['authorized'] = True
        return {'authorized': True, 'session': 'SESSION-STRING', 'me': ME}, None
    if not state['authorized']:
        return None, {'code': 'auth', 'message': 'Вход в аккаунт больше не действует. Войди заново.'}
    if method == 'dialogs':
        query = str(p.get('query') or '').lower().lstrip('@')
        return {'chats': [c for c in CFG.get('chats') or [] if query in (c.get('title', '') + ' @' + c.get('username', '')).lower()][:p.get('limit') or 30]}, None
    if method == 'resolve':
        ref = str(p.get('chat')).lstrip('@').lower()
        chat = next((c for c in CFG.get('chats') or [] if c.get('username', '').lower() == ref or str(c['id']) == ref), None)
        return ({'chat': chat}, None) if chat else (None, {'code': 'not_found', 'message': 'Чат не найден.'})
    if method == 'history':
        return {'chat': next((c for c in CFG.get('chats') or [] if c['id'] == p.get('chat')), {'id': p.get('chat'), 'title': ''}), 'messages': (CFG.get('history') or {}).get(str(p.get('chat')), [])}, None
    if method == 'send':
        state['next'] += 1
        return {'ids': [state['next']]}, None
    if method == 'download':
        return {'data': CFG.get('file') or '', 'name': 'photo.png', 'mime': 'image/png', 'kind': 'photo'}, None
    if method == 'raw':
        return {'result': '{"ok": true}', 'truncated': False}, None
    if method == 'chat' and p.get('action') in ('join', 'create_group', 'create_channel'):
        return {'chat': {'id': -100500, 'type': 'supergroup', 'title': str(p.get('value'))}}, None
    if method == 'chat' and p.get('action') == 'info':
        return {'chat': {'id': p.get('chat'), 'title': 'Чат', 'about': 'описание'}}, None
    if method == 'account' and p.get('action') == 'me':
        return {'me': ME}, None
    if method in ('message', 'chat', 'account', 'logout'):
        return {'done': True}, None
    return None, {'code': 'bad_request', 'message': 'Неизвестный метод.'}


emit({'event': 'hello', 'protocol': 1})
for line in sys.stdin:
    try:
        request = json.loads(line)
    except ValueError:
        continue
    method = request.get('method')
    params = request.get('params') or {}
    emit({'event': '_log', 'entry': {'method': method, 'params': {k: v for k, v in params.items() if k not in ('api_hash',)}}})
    if method == '_push':
        emit({'event': 'message', 'message': params['message']})
        emit({'id': request.get('id'), 'result': {'done': True}})
        continue
    if method == '_exit':
        sys.exit(3)
    result, error = handle(method, params)
    emit({'id': request.get('id'), 'error': error} if error else {'id': request.get('id'), 'result': result})
