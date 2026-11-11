"""Помощник аккаунта Telegram: отдельный процесс на Telethon, ничего из Бендера не импортирует.

Протокол — строки JSON. Вход: {"id":n,"method":"…","params":{…}}. Выход: {"id":n,"result":{…}} или
{"id":n,"error":{"code":"…","message":"…"}}; события без id: {"event":"message","message":{…}}.
Сессия и ключи приходят первым запросом configure по stdin: ни в аргументах, ни в окружении, ни на диске их нет.
Идентификаторы чатов — «помеченные», как в Bot API: пользователь 123, группа -456, супергруппа/канал -100….
"""
import asyncio
import base64
import datetime
import io
import json
import sys

from telethon import TelegramClient, errors, events, functions, types, utils
from telethon.sessions import StringSession

MAX_FILE = 20 * 1024 * 1024
MAX_RESULT = 400_000
client = None
login = {}
out_lock = None


def emit(obj):
    data = (json.dumps(obj, ensure_ascii=False, default=str) + '\n').encode()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


class Bad(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def need():
    if client is None:
        raise Bad('not_configured', 'Помощник не настроен.')
    return client


def person(u):
    if u is None:
        return None
    if isinstance(u, types.User):
        return {'id': u.id, 'name': ' '.join(x for x in (u.first_name, u.last_name) if x) or (u.username or str(u.id)), 'username': u.username or '', 'bot': bool(u.bot)}
    return {'id': utils.get_peer_id(u), 'name': getattr(u, 'title', '') or '', 'username': getattr(u, 'username', '') or '', 'bot': False}


def chat_info(e):
    if e is None:
        return None
    if isinstance(e, types.User):
        return {**person(e), 'type': 'bot' if e.bot else 'user', 'title': person(e)['name']}
    kind = 'group'
    if isinstance(e, types.Channel):
        kind = 'supergroup' if e.megagroup else 'channel'
    return {'id': utils.get_peer_id(e), 'type': kind, 'title': getattr(e, 'title', '') or '', 'username': getattr(e, 'username', '') or ''}


def media_info(m):
    if not m.media or isinstance(m.media, types.MessageMediaWebPage):
        return None
    kind = 'photo' if m.photo else 'voice' if m.voice else 'video_note' if m.video_note else 'sticker' if m.sticker else 'gif' if m.gif else 'video' if m.video else 'audio' if m.audio else 'document' if m.document else \
        'poll' if m.poll else 'contact' if m.contact else 'location' if m.geo else 'other'
    f = m.file
    info = {'kind': kind}
    if f is not None and kind not in ('poll', 'contact', 'location', 'other'):
        info.update(name=f.name or '', size=f.size or 0, mime=f.mime_type or '')
        if kind == 'sticker' and f.emoji:
            info['emoji'] = f.emoji
    if m.poll:
        info['question'] = getattr(getattr(m.poll.poll, 'question', None), 'text', None) or str(getattr(m.poll.poll, 'question', ''))
    return info


async def msg_info(m, chat=None):
    if chat is None:
        chat = await m.get_chat()
    sender = await m.get_sender()
    text = m.raw_text or ''
    if m.action is not None and not text:
        text = '[служебное: ' + type(m.action).__name__.replace('MessageAction', '') + ']'
    return {'id': m.id, 'chat': chat_info(chat), 'sender': person(sender) or chat_info(chat), 'text': text, 'date': m.date.timestamp() if m.date else 0, 'out': bool(m.out),
            'reply_to': m.reply_to_msg_id, 'mentioned': bool(m.mentioned), 'media': media_info(m), 'edited': bool(m.edit_date), 'group': m.grouped_id,
            'forward': bool(m.forward), 'service': m.action is not None}


async def resolve(ref):
    c = need()
    if ref in ('me', 'self', 'я'):
        return 'me'
    if isinstance(ref, str) and ref.lstrip('-').isdigit():
        ref = int(ref)
    if isinstance(ref, bool) or not isinstance(ref, (int, str)) or ref == '':
        raise Bad('bad_request', 'Нужен id чата, @имя или ссылка.')
    try:
        return await c.get_input_entity(ref)
    except ValueError:
        await c.get_dialogs(limit=300)
        try:
            return await c.get_input_entity(ref)
        except ValueError:
            raise Bad('not_found', 'Чат не найден: ' + str(ref) + '. Возьми id из списка чатов или укажи @имя.') from None


def stream(item):
    raw = base64.b64decode(item['data'], validate=True)
    if len(raw) > MAX_FILE:
        raise Bad('bad_request', 'Файл больше 20 МБ.')
    buf = io.BytesIO(raw)
    buf.name = str(item.get('name') or 'file')
    return buf


async def on_message(event, edited=False):
    try:
        emit({'event': 'message', 'message': {**await msg_info(event.message), 'edited': edited}})
    except Exception as error:  # noqa: BLE001 — одно непонятное обновление не роняет поток
        print('event failed:', repr(error), file=sys.stderr, flush=True)


async def me_info():
    return person(await need().get_me())


async def ready():
    c = need()
    c.add_event_handler(on_message, events.NewMessage())
    c.add_event_handler(lambda e: on_message(e, True), events.MessageEdited())
    try:
        await c.get_dialogs(limit=200)  # память имён и адресов: StringSession её между запусками не хранит
    except errors.RPCError:
        pass


async def configure(p):
    global client
    if client is not None:
        await client.disconnect()
    client = TelegramClient(StringSession(p.get('session') or ''), int(p['api_id']), str(p['api_hash']), device_model='Bender', system_version='userbot', app_version='1',
                            flood_sleep_threshold=20, auto_reconnect=True, timeout=10, connection_retries=3)
    try:
        await asyncio.wait_for(client.connect(), 45)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        raise Bad('network', 'Нет связи с серверами Telegram. Проверь интернет: аккаунту нужен прямой доступ к Telegram, не только к сайтам.') from None
    if not await client.is_user_authorized():
        return {'authorized': False}
    await ready()
    return {'authorized': True, 'me': await me_info()}


async def login_start(p):
    sent = await need().send_code_request(str(p['phone']))
    login.update(phone=str(p['phone']), hash=sent.phone_code_hash)
    return {'sent': True, 'via': type(sent.type).__name__.replace('SentCodeType', '').lower()}


async def logged():
    await ready()
    return {'authorized': True, 'session': client.session.save(), 'me': await me_info()}


async def login_code(p):
    if not login:
        raise Bad('bad_request', 'Сначала запроси код.')
    try:
        await need().sign_in(login['phone'], str(p['code']).strip(), phone_code_hash=login['hash'])
    except errors.SessionPasswordNeededError:
        return {'authorized': False, 'password_needed': True}
    return await logged()


async def login_password(p):
    await need().sign_in(password=str(p['password']))
    return await logged()


async def logout(p):
    await need().log_out()
    return {'done': True}


async def dialogs(p):
    c = need()
    query = str(p.get('query') or '').strip().lower()
    limit = max(1, min(int(p.get('limit') or 30), 200))
    rows = []
    async for d in c.iter_dialogs(limit=None if query else limit, archived=None if p.get('archived') else False):
        info = chat_info(d.entity)
        if query and query.lstrip('@') not in (info['title'] + ' @' + info['username']).lower():
            continue
        rows.append({**info, 'unread': d.unread_count, 'last': d.date.timestamp() if d.date else 0, 'muted': bool(getattr(d.dialog.notify_settings, 'mute_until', None))})
        if len(rows) >= limit:
            break
    if query and not rows and (query.startswith('@') or 't.me/' in query):
        try:
            rows.append({**chat_info(await c.get_entity(str(p['query']).strip())), 'unread': 0, 'last': 0, 'muted': False, 'found_by_name': True})
        except (ValueError, errors.RPCError):
            pass
    return {'chats': rows}


async def history(p):
    c = need()
    entity = await resolve(p['chat'])
    chat = await c.get_entity(entity)
    rows = []
    async for m in c.iter_messages(entity, limit=max(1, min(int(p.get('limit') or 30), 100)), offset_id=int(p.get('before') or 0), search=str(p['query']) if p.get('query') else None,
                                   ids=[int(x) for x in p['ids']] if p.get('ids') else None):
        if m is not None:
            rows.append(await msg_info(m, chat))
    return {'chat': chat_info(chat), 'messages': rows}


async def send(p):
    c = need()
    entity = await resolve(p['chat'])
    when = datetime.datetime.fromisoformat(p['schedule']) if p.get('schedule') else None
    common = {'reply_to': int(p['reply_to']) if p.get('reply_to') else None, 'silent': bool(p.get('silent')) or None, 'schedule': when}
    files = [stream(item) for item in p.get('files') or []]
    text = str(p.get('text') or '')
    if files:
        kind = p.get('kind') or 'auto'
        sent = await c.send_file(entity, files if len(files) > 1 else files[0], caption=text or None, voice_note=kind == 'voice', video_note=kind == 'video_note',
                                 force_document=kind == 'document', **common)
    else:
        if not text.strip():
            raise Bad('bad_request', 'Пустое сообщение.')
        sent = await c.send_message(entity, text, link_preview=bool(p.get('preview', True)), **common)
    sent = sent if isinstance(sent, list) else [sent]
    return {'ids': [m.id for m in sent if m is not None]}


async def message(p):
    c = need()
    action = p.get('action')
    entity = await resolve(p['chat'])
    ids = [int(x) for x in (p.get('ids') or [])]
    if action != 'read' and not ids:
        raise Bad('bad_request', 'Нужен номер сообщения.')
    if action == 'edit':
        await c.edit_message(entity, ids[0], str(p.get('text') or ''))
    elif action == 'delete':
        await c.delete_messages(entity, ids, revoke=True)
    elif action == 'react':
        emoji = str(p.get('emoji') or '')
        await c(functions.messages.SendReactionRequest(peer=entity, msg_id=ids[0], reaction=[types.ReactionEmoji(emoticon=emoji)] if emoji else []))
    elif action == 'forward':
        sent = await c.forward_messages(await resolve(p['to']), ids, entity)
        return {'ids': [m.id for m in (sent if isinstance(sent, list) else [sent]) if m is not None]}
    elif action == 'pin':
        await c.pin_message(entity, ids[0], notify=bool(p.get('notify')))
    elif action == 'unpin':
        await c.unpin_message(entity, ids[0])
    elif action == 'read':
        await c.send_read_acknowledge(entity, max_id=ids[0] if ids else None, clear_mentions=True)
    else:
        raise Bad('bad_request', 'Неизвестное действие с сообщением.')
    return {'done': True}


async def download(p):
    c = need()
    entity = await resolve(p['chat'])
    m = await c.get_messages(entity, ids=int(p['id']))
    if m is None or not m.media or m.file is None:
        raise Bad('not_found', 'В этом сообщении нет файла.')
    limit = min(int(p.get('limit') or MAX_FILE), MAX_FILE)
    if (m.file.size or 0) > limit:
        raise Bad('too_big', 'Файл больше предела: ' + str(m.file.size) + ' байт.')
    raw = await c.download_media(m, bytes)
    if not raw or len(raw) > limit:
        raise Bad('too_big', 'Файл пуст или больше предела.')
    info = media_info(m) or {}
    return {'data': base64.b64encode(raw).decode(), 'name': m.file.name or (info.get('kind', 'file') + (m.file.ext or '')), 'mime': m.file.mime_type or '', 'kind': info.get('kind', 'document')}


async def resolve_chat(p):
    return {'chat': chat_info(await need().get_entity(await resolve(p['chat'])))}


async def photo_input(p):
    files = p.get('files') or []
    if not files:
        raise Bad('bad_request', 'Нужен файл картинки.')
    return await need().upload_file(stream(files[0]))


async def chat(p):
    c = need()
    action = p.get('action')
    value = p.get('value')
    if action == 'join':
        ref = str(value or p.get('chat') or '').strip()
        tail = ref.rsplit('/', 1)[-1]
        if '/joinchat/' in ref or tail.startswith('+'):
            result = await c(functions.messages.ImportChatInviteRequest(tail.lstrip('+')))
        else:
            result = await c(functions.channels.JoinChannelRequest(await resolve(ref)))
        chats = getattr(result, 'chats', None) or []
        return {'chat': chat_info(chats[0]) if chats else None}
    if action in ('create_group', 'create_channel'):
        result = await c(functions.channels.CreateChannelRequest(title=str(value or ''), about=str(p.get('about') or ''), megagroup=action == 'create_group', broadcast=action == 'create_channel'))
        return {'chat': chat_info(result.chats[0])}
    entity = await resolve(p['chat'])
    full = await c.get_entity(entity)
    channel = isinstance(full, types.Channel)
    if action == 'info':
        info = chat_info(full)
        if isinstance(full, types.User):
            more = (await c(functions.users.GetFullUserRequest(entity))).full_user
            info.update(about=more.about or '', phone=full.phone or '', contact=bool(full.contact), blocked=bool(more.blocked), last_seen=type(full.status).__name__.replace('UserStatus', '') if full.status else '')
        elif channel:
            more = (await c(functions.channels.GetFullChannelRequest(entity))).full_chat
            info.update(about=more.about or '', members=more.participants_count, admin=bool(full.admin_rights or full.creator), linked_chat=more.linked_chat_id)
        else:
            more = (await c(functions.messages.GetFullChatRequest(full.id))).full_chat
            info.update(about=more.about or '', members=getattr(full, 'participants_count', None), admin=bool(full.admin_rights or full.creator))
        return {'chat': info}
    if action == 'members':
        rows = []
        async for u in c.iter_participants(entity, limit=max(1, min(int(p.get('limit') or 50), 200)), search=str(value or '')):
            rows.append({**person(u), 'admin': type(getattr(u, 'participant', None)).__name__ in ('ChannelParticipantAdmin', 'ChannelParticipantCreator', 'ChatParticipantAdmin', 'ChatParticipantCreator')})
        return {'members': rows}
    if action == 'leave':
        await c.delete_dialog(entity)
    elif action in ('mute', 'unmute'):
        await c(functions.account.UpdateNotifySettingsRequest(peer=types.InputNotifyPeer(entity), settings=types.InputPeerNotifySettings(mute_until=2 ** 31 - 1 if action == 'mute' else 0)))
    elif action in ('archive', 'unarchive'):
        await c.edit_folder(entity, 1 if action == 'archive' else 0)
    elif action == 'title':
        await c(functions.channels.EditTitleRequest(entity, str(value)) if channel else functions.messages.EditChatTitleRequest(full.id, str(value)))
    elif action == 'about':
        await c(functions.messages.EditChatAboutRequest(entity, str(value or '')))
    elif action == 'photo':
        photo = types.InputChatUploadedPhoto(await photo_input(p))
        await c(functions.channels.EditPhotoRequest(entity, photo) if channel else functions.messages.EditChatPhotoRequest(full.id, photo))
    elif action == 'invite':
        user = await resolve(value)
        await c(functions.channels.InviteToChannelRequest(entity, [user]) if channel else functions.messages.AddChatUserRequest(full.id, user, fwd_limit=0))
    elif action == 'kick':
        await c.kick_participant(entity, await resolve(value))
    elif action == 'link':
        return {'link': (await c(functions.messages.ExportChatInviteRequest(entity))).link}
    elif action == 'typing':
        await c(functions.messages.SetTypingRequest(entity, types.SendMessageTypingAction()))
    elif action in ('block', 'unblock'):
        await c(functions.contacts.BlockRequest(entity) if action == 'block' else functions.contacts.UnblockRequest(entity))
    else:
        raise Bad('bad_request', 'Неизвестное действие с чатом.')
    return {'done': True}


async def account(p):
    c = need()
    action = p.get('action')
    value = p.get('value')
    if action == 'me':
        me = await c.get_me()
        full = (await c(functions.users.GetFullUserRequest('me'))).full_user
        return {'me': {**person(me), 'phone': me.phone or '', 'about': full.about or '', 'premium': bool(me.premium)}}
    if action == 'name':
        first, _, last = str(value or '').strip().partition(' ')
        if not first:
            raise Bad('bad_request', 'Нужно имя.')
        await c(functions.account.UpdateProfileRequest(first_name=first, last_name=last))
    elif action == 'bio':
        await c(functions.account.UpdateProfileRequest(about=str(value or '')))
    elif action == 'username':
        await c(functions.account.UpdateUsernameRequest(str(value or '').lstrip('@')))
    elif action == 'photo':
        await c(functions.photos.UploadProfilePhotoRequest(file=await photo_input(p)))
    elif action in ('online', 'offline'):
        await c(functions.account.UpdateStatusRequest(offline=action == 'offline'))
    elif action == 'contacts':
        result = await c(functions.contacts.GetContactsRequest(hash=0))
        return {'contacts': [person(u) for u in getattr(result, 'users', [])][:500]}
    else:
        raise Bad('bad_request', 'Неизвестное действие с аккаунтом.')
    return {'done': True}


async def build(value):
    """JSON → объекты Telegram: {"_":"InputPeerEmpty",…} — конструктор; {"$peer":чат} — адрес чата; {"$bytes":base64}."""
    if isinstance(value, list):
        return [await build(x) for x in value]
    if isinstance(value, dict):
        if set(value) == {'$peer'}:
            return await resolve(value['$peer'])
        if set(value) == {'$bytes'}:
            return base64.b64decode(value['$bytes'])
        if set(value) == {'$date'}:
            return datetime.datetime.fromisoformat(value['$date'])
        args = {k: await build(v) for k, v in value.items() if k != '_'}
        if '_' in value:
            cls = getattr(types, str(value['_']), None)
            if cls is None:
                raise Bad('bad_request', 'Нет типа Telegram ' + str(value['_']) + '.')
            return cls(**args)
        return args
    return value


async def raw(p):
    name = str(p.get('method') or '')
    space, _, method = name.rpartition('.')
    module = getattr(functions, space, None) if space else functions
    cls = getattr(module, method + 'Request', None) if module is not None and method else None
    if cls is None:
        raise Bad('bad_request', 'Нет метода Telegram ' + name + '. Формат: раздел.Метод, например messages.GetStickers.')
    args = await build(p.get('params') or {})
    if not isinstance(args, dict):
        raise Bad('bad_request', 'Параметры — объект.')
    result = await need()(cls(**args))
    data = result.to_dict() if hasattr(result, 'to_dict') else result
    text = json.dumps(data, ensure_ascii=False, default=lambda o: '<' + str(len(o)) + ' байт>' if isinstance(o, bytes) else str(o))
    return {'result': text[:MAX_RESULT], 'truncated': len(text) > MAX_RESULT}


METHODS = {'configure': configure, 'login_start': login_start, 'login_code': login_code, 'login_password': login_password, 'logout': logout, 'dialogs': dialogs, 'history': history,
           'send': send, 'message': message, 'download': download, 'resolve': resolve_chat, 'chat': chat, 'account': account, 'raw': raw}
AUTH = tuple(getattr(errors, name) for name in ('AuthKeyUnregisteredError', 'AuthKeyDuplicatedError', 'UserDeactivatedError', 'UserDeactivatedBanError', 'SessionRevokedError', 'SessionExpiredError', 'AuthKeyInvalidError')
             if hasattr(errors, name))
WORDS = {'PhoneNumberInvalidError': 'Telegram не принял номер. Укажи его с кодом страны, например +79001234567.', 'PhoneCodeInvalidError': 'Код не подошёл.', 'PhoneCodeExpiredError': 'Код устарел — запроси новый.',
         'PasswordHashInvalidError': 'Пароль двухэтапной проверки не подошёл.', 'ApiIdInvalidError': 'Telegram не принял api_id и api_hash. Проверь их на my.telegram.org.',
         'ChatWriteForbiddenError': 'В этот чат писать нельзя.', 'UserPrivacyRestrictedError': 'Настройки приватности человека это запрещают.', 'PeerFloodError': 'Telegram ограничил аккаунт за слишком частые действия. Сделай паузу.',
         'ChatAdminRequiredError': 'Нужны права администратора чата.', 'MessageNotModifiedError': 'Текст не изменился.', 'UsernameOccupiedError': 'Это имя уже занято.', 'PhoneNumberBannedError': 'Этот номер заблокирован в Telegram.'}


async def handle(request):
    rid = request.get('id')
    try:
        fn = METHODS.get(request.get('method'))
        if fn is None:
            raise Bad('bad_request', 'Неизвестный метод помощника.')
        emit({'id': rid, 'result': await fn(request.get('params') or {})})
    except Bad as error:
        emit({'id': rid, 'error': {'code': error.code, 'message': str(error)}})
    except errors.FloodWaitError as error:
        emit({'id': rid, 'error': {'code': 'flood', 'message': 'Telegram просит подождать ' + str(error.seconds) + ' с.', 'seconds': error.seconds}})
    except AUTH:
        emit({'id': rid, 'error': {'code': 'auth', 'message': 'Вход в аккаунт больше не действует. Войди заново.'}})
    except errors.RPCError as error:
        name = type(error).__name__
        emit({'id': rid, 'error': {'code': 'telegram', 'message': WORDS.get(name) or ('Telegram отказал: ' + name + (' — ' + str(error.message) if getattr(error, 'message', None) else '')), 'name': name}})
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        emit({'id': rid, 'error': {'code': 'bad_request', 'message': 'Неверный запрос: ' + type(error).__name__ + ': ' + str(error)[:300]}})
    except (ConnectionError, OSError, asyncio.TimeoutError) as error:
        emit({'id': rid, 'error': {'code': 'network', 'message': 'Нет связи с Telegram: ' + type(error).__name__}})


async def main():
    loop = asyncio.get_running_loop()
    emit({'event': 'hello', 'protocol': 1})
    tasks = set()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
        if not line:
            break
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if isinstance(request, dict):
            task = asyncio.ensure_future(handle(request))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    if client is not None:
        await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
