"""Тулы аккаунта Telegram агента (юзербот). Доступны, только когда хозяин подключил аккаунт этому агенту.

Шесть понятных действий и один запасной выход: чаты, чтение, отправка, действие с сообщением, действие с чатом,
действие с аккаунтом — и tg_call для всего остального API. Каждое проходит Предохранитель и список чатов хозяина.
"""
from __future__ import annotations

import base64
import json
import time

from ..core.errors import HumanError
from .registry import ToolContext, ToolResult
from .web import untrusted

MESSAGE = ('edit', 'delete', 'react', 'forward', 'pin', 'unpin', 'read')
CHAT_READ = ('info', 'members')
CHAT_OWN = ('typing', 'mute', 'unmute', 'archive', 'unarchive')
CHAT_ADMIN = ('title', 'about', 'photo', 'invite', 'kick', 'link', 'leave', 'block', 'unblock')
CHAT_NEW = ('join', 'create_group', 'create_channel')
ACCOUNT_READ = ('me', 'contacts')
ACCOUNT_WRITE = ('name', 'bio', 'username', 'photo', 'online', 'offline')
TYPES = {'user': 'человек', 'bot': 'бот', 'group': 'группа', 'supergroup': 'группа', 'channel': 'канал'}


def _on(agent_ctx) -> bool:
    bot = getattr(agent_ctx, '_userbot', None)
    return bool(bot and bot.ready())


def bad(title, hint):
    return HumanError('userbot', title, hint)


async def target(ctx, chat):
    """Чат → (числовой id, сведения). «owner» и «хозяин» — личный чат хозяина; @имя и ссылки узнаёт Telegram."""
    bot = ctx.app.userbot
    if not bot.ready():
        raise bad('Аккаунт Telegram не подключён', 'Его подключает хозяин в «Связях».')
    ref = str(chat).strip() if not isinstance(chat, bool) and chat is not None else ''
    if ref.lower() in ('owner', 'хозяин'):
        owner = bot.owner()
        if not owner:
            raise bad('Хозяин не привязан к аккаунту', 'Он привязывается кодом из «Связей».')
        return owner['user_id'], {'id': owner['user_id'], 'type': 'user', 'title': 'хозяин'}
    if ref in ('', 'here', 'сюда'):
        cid = bot.chat_of(ctx.session_id)
        if cid is None:
            raise bad('Не указан чат', 'Укажи id из tg_chats, @имя или owner.')
        return cid, {'id': cid}
    if ref.lstrip('-').isdigit():
        return int(ref), {'id': int(ref)}
    info = (await bot.link.call('resolve', {'chat': ref}, timeout=30)).get('chat') or {}
    if not isinstance(info.get('id'), int):
        raise bad('Чат не найден', 'Возьми id из tg_chats или укажи @имя.')
    return info['id'], info


def stamp(ts):
    return time.strftime('%d.%m %H:%M', time.localtime(ts or 0))


def line(m):
    sender = m.get('sender') or {}
    media = m.get('media') or {}
    extra = (' [' + str(media.get('kind')) + (' ' + str(media.get('name') or media.get('emoji') or media.get('question') or '')).rstrip() + ']') if media else ''
    return ('#' + str(m.get('id')) + ' · ' + stamp(m.get('date')) + ' · ' + ('я' if m.get('out') else str(sender.get('name') or sender.get('id')))
            + (' · ответ на #' + str(m['reply_to']) if m.get('reply_to') else '') + (' · правка' if m.get('edited') else '') + ': ' + str(m.get('text') or '') + extra)


async def attachments(ctx, paths):
    from pathlib import Path

    from . import files
    out = []
    for path in ([paths] if isinstance(paths, str) else list(paths or []))[:10]:
        p = await files.readable(ctx, str(path), 'отправить в Telegram')
        if p is None:
            raise bad('Нет такого файла', str(path))
        if p.stat().st_size > 20 * 1024 * 1024:
            raise bad('Файл больше 20 МБ', 'Через аккаунт уходит до 20 МБ на файл.')
        out.append({'name': Path(p).name, 'data': base64.b64encode(p.read_bytes()).decode()})
    return out


def register(reg) -> None:
    @reg.tool('tg_chats', 'Telegram: чаты аккаунта', group='userbot', available=_on)
    async def tg_chats(ctx: ToolContext, query: str = '', limit: int = 30) -> ToolResult:
        """Чаты твоего аккаунта Telegram: id, тип, название, непрочитанное и что тебе там разрешено. id нужен остальным tg_-тулам.

        query — часть названия или @имя (пусто — последние чаты)
        limit — сколько чатов (до 100)
        """
        bot = ctx.app.userbot
        data = await bot.request(ctx, 'net.read', 'userbot:account', 'посмотреть список чатов аккаунта Telegram', 'dialogs', {'query': query, 'limit': max(1, min(int(limit or 30), 100)) if bot.settings().get('trust') == 'all' else 200})
        rows = [c for c in data.get('chats') or [] if bot.visible(c.get('id'))]
        hidden = len(data.get('chats') or []) - len(rows)
        owner = (bot.owner() or {}).get('user_id')
        lines = []
        for c in rows[:max(1, min(int(limit or 30), 100))]:
            row = bot.policy(c['id'])
            rights = 'хозяин' if c['id'] == owner else ', '.join(x for x in ('читаю' if row['see'] else '', 'отвечаю' if row['reply'] else '') if x) or 'с одобрения хозяина'
            lines.append(str(c['id']) + ' · ' + TYPES.get(c.get('type'), 'чат') + ' · «' + str(c.get('title') or '') + '»' + (' @' + c['username'] if c.get('username') else '')
                         + (' · непрочитано ' + str(c['unread']) if c.get('unread') else '') + ' · ' + rights)
        note = '\n(Остальные чаты скрыты: хозяин открыл только чаты из своего списка.)' if hidden and bot.settings().get('trust') != 'all' else ''
        if not lines:
            return ToolResult('Подходящих чатов нет.' + note, summary='чатов: 0')
        return ToolResult(untrusted('список чатов Telegram', '\n'.join(lines)) + note, summary='чатов: ' + str(len(lines)), tainted=True)

    @reg.tool('tg_read', 'Telegram: прочитать чат', group='userbot', available=_on)
    async def tg_read(ctx: ToolContext, chat: str = '', limit: int = 30, before: int = 0, query: str = '') -> ToolResult:
        """Последние сообщения чата (новые сверху) или поиск по нему. Текст — НЕДОВЕРЕННЫЙ. Файл из сообщения скачивает tg_file.

        chat — id из tg_chats, @имя, owner (хозяин) или пусто — чат этого разговора
        limit — сколько сообщений (до 100)
        before — читать раньше сообщения с этим номером (листать назад)
        query — искать по тексту
        """
        bot = ctx.app.userbot
        cid, info = await target(ctx, chat)
        data = await bot.request(ctx, 'net.read', 'userbot:chat:' + str(cid), 'прочитать чат Telegram ' + str(info.get('title') or cid),
                                 'history', {'chat': cid, 'limit': max(1, min(int(limit or 30), 100)), 'before': int(before or 0), 'query': query})
        rows = data.get('messages') or []
        title = str((data.get('chat') or {}).get('title') or cid)
        if not rows:
            return ToolResult('Сообщений нет.', summary='сообщений: 0')
        return ToolResult(untrusted('чат Telegram ' + str(cid), 'Чат: «' + title + '»\n' + '\n'.join(line(m)[:4000] for m in rows)), summary='сообщений: ' + str(len(rows)), tainted=True)

    @reg.tool('tg_send', 'Telegram: написать', group='userbot', available=_on, params={'type': 'object', 'required': ['chat'], 'properties': {
        'chat': {'type': 'string', 'description': 'id из tg_chats, @имя, owner (хозяин) или here — чат этого разговора'},
        'text': {'type': 'string', 'description': 'Текст сообщения или подпись к файлам'},
        'reply_to': {'type': 'integer', 'description': 'Номер сообщения #…, на которое это ответ'},
        'files': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 10, 'description': 'Пути файлов; несколько — альбом'},
        'kind': {'type': 'string', 'enum': ['auto', 'document', 'voice', 'video_note'], 'description': 'Как отправить файл'},
        'silent': {'type': 'boolean', 'description': 'Без звука'},
        'schedule': {'type': 'string', 'description': 'Отложить: дата и время ISO, например 2026-11-12T09:00:00+03:00'}}})
    async def tg_send(ctx: ToolContext, chat: str, text: str = '', reply_to: int = 0, files: list | None = None, kind: str = 'auto', silent: bool = False, schedule: str = '') -> ToolResult:
        """Написать от своего аккаунта в чат: текст, файлы, ответ на сообщение, без звука, отложенно. Обычный ответ в чат разговора слать не нужно — его доставит итог хода.

        chat — куда
        text — текст
        """
        bot = ctx.app.userbot
        cid, info = await target(ctx, chat)
        payload = await attachments(ctx, files)
        if not text.strip() and not payload:
            raise bad('Сообщение пустое', 'Нужен текст или файл.')
        data = await bot.request(ctx, 'msg.send', 'userbot:chat:' + str(cid), 'написать в Telegram ' + str(info.get('title') or cid) + ': «' + text[:80] + '»', 'send',
                                 {'chat': cid, 'text': text, 'reply_to': int(reply_to or 0), 'files': payload, 'kind': kind, 'silent': bool(silent), 'schedule': schedule},
                                 detail={'preview': text[:1500], 'files': [f['name'] for f in payload]}, timeout=300)
        ids = data.get('ids') or []
        return ToolResult('Отправлено' + (': ' + ', '.join('#' + str(i) for i in ids) if ids else '') + '.', summary='✈ ' + str(info.get('title') or cid)[:40], data={'ids': ids, 'chat': cid})

    @reg.tool('tg_message', 'Telegram: действие с сообщением', group='userbot', available=_on, params={'type': 'object', 'required': ['action', 'chat'], 'properties': {
        'action': {'type': 'string', 'enum': list(MESSAGE), 'description': 'edit — править своё; delete — удалить; react — реакция; forward — переслать; pin, unpin; read — отметить чат прочитанным'},
        'chat': {'type': 'string', 'description': 'id, @имя, owner или here'},
        'message': {'anyOf': [{'type': 'integer'}, {'type': 'array', 'items': {'type': 'integer'}, 'maxItems': 100}], 'description': 'Номер сообщения или список номеров'},
        'text': {'type': 'string', 'description': 'Новый текст для edit'},
        'emoji': {'type': 'string', 'description': 'Реакция для react; пусто — снять'},
        'to': {'type': 'string', 'description': 'Куда переслать (forward)'}}})
    async def tg_message(ctx: ToolContext, action: str, chat: str, message=None, text: str = '', emoji: str = '', to: str = '') -> ToolResult:
        """Править, удалить, переслать, закрепить сообщение, поставить реакцию или отметить чат прочитанным."""
        if action not in MESSAGE:
            raise bad('Неизвестное действие', 'Доступно: ' + ', '.join(MESSAGE) + '.')
        bot = ctx.app.userbot
        cid, info = await target(ctx, chat)
        ids = [int(x) for x in (message if isinstance(message, list) else [message] if message else [])]
        params = {'action': action, 'chat': cid, 'ids': ids, 'text': text, 'emoji': emoji}
        name = str(info.get('title') or cid)
        if action == 'forward':
            to_id, to_info = await target(ctx, to)
            await ctx.check('net.read', 'userbot:chat:' + str(cid), 'взять сообщение из чата ' + name)
            params['to'] = to_id
            data = await bot.request(ctx, 'msg.send', 'userbot:chat:' + str(to_id), 'переслать сообщение в Telegram ' + str(to_info.get('title') or to_id), 'message', params)
            return ToolResult('Переслано.', summary='переслано', data=data)
        capability = 'net.read' if action == 'read' else 'msg.send'
        await bot.request(ctx, capability, 'userbot:chat:' + str(cid), {'edit': 'править сообщение', 'delete': 'удалить сообщение', 'react': 'поставить реакцию', 'pin': 'закрепить сообщение',
                                                                      'unpin': 'открепить сообщение', 'read': 'отметить прочитанным'}[action] + ' в Telegram ' + name, 'message', params,
                          detail={'preview': text[:1500]} if text else None)
        return ToolResult('Готово.', summary=action + ' ' + name[:40])

    @reg.tool('tg_file', 'Telegram: скачать файл', group='userbot', available=_on)
    async def tg_file(ctx: ToolContext, chat: str, message: int) -> ToolResult:
        """Скачать файл или фото из сообщения во входящие (до 20 МБ). Дальше — read_file или look по выданному пути.

        chat — id, @имя, owner или here
        message — номер сообщения #…
        """
        from ..channels import media
        bot = ctx.app.userbot
        cid, info = await target(ctx, chat)
        limit = min(int(bot.settings().get('max_file_mb') or 20), int(ctx.app.settings.get('attachments', 'max_mb', 20))) * 1024 * 1024
        data = await bot.request(ctx, 'net.read', 'userbot:chat:' + str(cid), 'скачать файл из чата Telegram ' + str(info.get('title') or cid), 'download',
                                 {'chat': cid, 'id': int(message), 'limit': limit}, timeout=300)
        raw = base64.b64decode(data['data'], validate=True)
        item = await media.save(ctx.app, data.get('name') or 'файл', raw, kind='photo' if data.get('kind') == 'photo' else 'document', source='userbot', session=ctx.session_id,
                                actor=ctx.actor, unattended=ctx.unattended, tainted=True, why='сохранить файл из Telegram-аккаунта')
        ctx.tainted = True
        return ToolResult('Файл сохранён (недоверенные данные): ' + str(item['path']), summary='файл из Telegram', tainted=True, data={'path': item['path']})

    @reg.tool('tg_chat', 'Telegram: действие с чатом', group='userbot', available=_on, params={'type': 'object', 'required': ['action'], 'properties': {
        'action': {'type': 'string', 'enum': list(CHAT_READ + CHAT_OWN + CHAT_ADMIN + CHAT_NEW),
                   'description': 'info — сведения; members — участники; typing — «печатает»; mute/unmute; archive/unarchive; title, about, photo — оформление; invite, kick — участники; link — ссылка-приглашение; '
                                  'join — вступить (value: @имя или ссылка); leave — выйти; block/unblock; create_group, create_channel — создать (value: название)'},
        'chat': {'type': 'string', 'description': 'id, @имя, owner или here; для join и create не нужен'},
        'value': {'type': 'string', 'description': 'Название, описание, @имя участника, ссылка или строка поиска участников'},
        'file': {'type': 'string', 'description': 'Путь картинки для photo'}}})
    async def tg_chat(ctx: ToolContext, action: str, chat: str = '', value: str = '', file: str = '') -> ToolResult:
        """Сведения о чате и участниках, «печатает», звук и архив, оформление, приглашения, вступить, выйти, создать группу или канал."""
        bot = ctx.app.userbot
        if action in CHAT_NEW:
            data = await bot.request(ctx, 'msg.send', 'userbot:account', {'join': 'вступить в чат Telegram ', 'create_group': 'создать группу Telegram ', 'create_channel': 'создать канал Telegram '}[action] + value[:80],
                                     'chat', {'action': action, 'value': value})
            made = data.get('chat') or {}
            return ToolResult('Готово: чат ' + str(made.get('id') or '') + '. Читать и писать в нём без вопросов можно после того, как хозяин внесёт его в список.', summary=action, data=data)
        if action not in CHAT_READ + CHAT_OWN + CHAT_ADMIN:
            raise bad('Неизвестное действие', 'Смотри список действий в описании тула.')
        cid, info = await target(ctx, chat)
        name = str(info.get('title') or cid)
        params = {'action': action, 'chat': cid, 'value': value, 'files': await attachments(ctx, file) if file else []}
        if action in CHAT_READ:
            data = await bot.request(ctx, 'net.read', 'userbot:chat:' + str(cid), 'посмотреть ' + ('участников' if action == 'members' else 'сведения') + ' чата Telegram ' + name, 'chat', params)
            return ToolResult(untrusted('чат Telegram ' + str(cid), json.dumps(data.get('chat') or data.get('members') or {}, ensure_ascii=False, indent=1)[:20000]), summary=action + ' ' + name[:40], tainted=True)
        if action in CHAT_OWN:
            await bot.request(ctx, 'msg.send', 'userbot:chat:' + str(cid), action + ' в чате Telegram ' + name, 'chat', params)
            return ToolResult('Готово.', summary=action + ' ' + name[:40])
        data = await bot.request(ctx, 'msg.send', 'userbot:account', action + ' в чате Telegram ' + name + (': ' + value[:80] if value else ''), 'chat', params, detail={'chat': cid})
        return ToolResult('Готово.' + (' Ссылка: ' + str(data['link']) if data.get('link') else ''), summary=action + ' ' + name[:40], data=data)

    @reg.tool('tg_account', 'Telegram: мой аккаунт', group='userbot', available=_on, params={'type': 'object', 'required': ['action'], 'properties': {
        'action': {'type': 'string', 'enum': list(ACCOUNT_READ + ACCOUNT_WRITE), 'description': 'me — кто я; contacts — контакты; name, bio, username, photo — профиль; online, offline — статус'},
        'value': {'type': 'string', 'description': 'Имя и фамилия, текст «о себе» или @имя'},
        'file': {'type': 'string', 'description': 'Путь картинки для photo'}}})
    async def tg_account(ctx: ToolContext, action: str, value: str = '', file: str = '') -> ToolResult:
        """Свой профиль Telegram: посмотреть, сменить имя, «о себе», @имя и аватар, показать себя в сети."""
        bot = ctx.app.userbot
        if action not in ACCOUNT_READ + ACCOUNT_WRITE:
            raise bad('Неизвестное действие', 'Доступно: ' + ', '.join(ACCOUNT_READ + ACCOUNT_WRITE) + '.')
        params = {'action': action, 'value': value, 'files': await attachments(ctx, file) if file else []}
        if action in ACCOUNT_READ:
            data = await bot.request(ctx, 'net.read', 'userbot:account', 'посмотреть ' + ('контакты' if action == 'contacts' else 'профиль') + ' аккаунта Telegram', 'account', params)
            body = json.dumps(data.get('me') or data.get('contacts') or {}, ensure_ascii=False, indent=1)[:20000]
            return ToolResult(untrusted('контакты Telegram', body), summary='контакты', tainted=True) if action == 'contacts' else ToolResult(body, summary='мой аккаунт')
        await bot.request(ctx, 'msg.send', 'userbot:account', 'изменить аккаунт Telegram: ' + action + (' → ' + value[:80] if value else ''), 'account', params)
        return ToolResult('Готово.', summary='аккаунт: ' + action)

    @reg.tool('tg_call', 'Telegram: прямой вызов API', group='userbot', available=_on, params={'type': 'object', 'required': ['method'], 'properties': {
        'method': {'type': 'string', 'description': 'Метод API Telegram: раздел.Метод, например messages.GetStickers или account.GetAuthorizations'},
        'params': {'type': 'object', 'description': 'Параметры метода. Чат: {"$peer": id или "@имя"}; тип Telegram: {"_": "ИмяТипа", …}; байты: {"$bytes": base64}; дата: {"$date": ISO}'}}})
    async def tg_call(ctx: ToolContext, method: str, params: dict | None = None) -> ToolResult:
        """Запасной выход: любой метод API Telegram, которого нет в остальных tg_-тулах (опросы, стикеры, истории, папки, настройки приватности…). Каждый вызов подтверждает хозяин."""
        bot = ctx.app.userbot
        if not isinstance(method, str) or not method.replace('.', '').isalnum() or len(method) > 80:
            raise bad('Неверное имя метода', 'Формат: раздел.Метод, например messages.GetStickers.')
        data = await bot.request(ctx, 'msg.send', 'userbot:raw:' + method, 'вызвать метод Telegram ' + method, 'raw', {'method': method, 'params': params or {}},
                                 detail={'params': json.dumps(params or {}, ensure_ascii=False)[:1500]}, timeout=120)
        ctx.tainted = True
        return ToolResult(untrusted('ответ Telegram на ' + method, str(data.get('result') or '')[:60000]) + ('\n…[ответ обрезан]' if data.get('truncated') else ''), summary=method, tainted=True)
