"""Пересланное в Telegram: от кого, откуда и когда написано — для модели, пульта и групп.

Всё, что здесь собирается из чужих полей (имя отправителя, название канала, подпись автора),
управляется посторонним человеком. Поэтому оно попадает к модели только в строку источника
недоверенной рамки или внутрь неё (untrusted() ещё раз снимает переводы строк, рамки и спецтокены),
и никогда — в служебный текст вне рамки.

Пересланное остаётся недоверенным даже от самого хозяина или от этого бота: в прежнем сообщении
могло быть чужое (решение хозяина нужно, чтобы это ослабить).

Дата — ОРИГИНАЛЬНАЯ дата сообщения («написано пт 21.11 14:03»), без квадратных скобок: системные
метки времени модели выглядят как «[пт 21.11 14:03]» и означают время получения.
"""
from __future__ import annotations

import re

from ..core.dates import DAYS, local

MEDIA = ('photo', 'document', 'video', 'audio', 'animation', 'voice', 'video_note', 'sticker')
WORDS = {'photo': 'фото', 'document': 'документ', 'video': 'видео', 'audio': 'аудио', 'animation': 'гифка', 'voice': 'голосовое',
         'video_note': 'кружок', 'sticker': 'стикер', 'location': 'место', 'venue': 'место', 'contact': 'контакт', 'poll': 'опрос', 'dice': 'кубик'}
LEGACY = ('forward_origin', 'forward_from', 'forward_from_chat', 'forward_sender_name', 'forward_date')
NAME_MAX = 60


def clean_name(value, limit=NAME_MAX):
    """Одна строка без управляющих символов, рамок и спецтокенов; «ёлочки» не ломают наши кавычки."""
    from ..tools.web import clean_tokens
    text = re.sub(r'[\x00-\x1f\x7f\u2028\u2029]+', ' ', clean_tokens(str(value or '')))
    text = text.replace('<<<', '‹‹‹').replace('>>>', '›››').replace('«', '"').replace('»', '"')
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _person(user):
    name = ' '.join(str(user.get(k) or '').strip() for k in ('first_name', 'last_name')).strip()
    return name or str(user.get('title') or '').strip()


def _number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def link(chat, message_id):
    """t.me-ссылка на пост: публичный — по имени, закрытый канал — t.me/c/<номер без -100>/<пост>."""
    message_id = _number(message_id)
    if not isinstance(chat, dict) or message_id is None:
        return None
    username = re.sub(r'[^A-Za-z0-9_]', '', str(chat.get('username') or ''))
    if username:
        return 't.me/' + username + '/' + str(message_id)
    ident = _number(chat.get('id'))
    if ident is not None and str(ident).startswith('-100'):
        return 't.me/c/' + str(ident)[4:] + '/' + str(message_id)
    return None


def _from_chat(info, chat):
    chat = chat if isinstance(chat, dict) else {}
    info.update(name=clean_name(chat.get('title') or chat.get('username') or ''), username=clean_name(chat.get('username') or '', 40),
                id=_number(chat.get('id')))


def origin(msg, owner_id=None, bot_id=None):
    """Откуда пересланное: dict или None. Понимает forward_origin (Bot API 7+), прежние forward_* и
    is_automatic_forward (пост канала, сам пришедший в группу обсуждения)."""
    if not isinstance(msg, dict):
        return None
    raw = msg.get('forward_origin')
    info = None
    if isinstance(raw, dict) and raw.get('type'):
        kind = str(raw['type'])
        info = {'kind': kind, 'date': raw.get('date')}
        if kind == 'user':
            user = raw.get('sender_user') if isinstance(raw.get('sender_user'), dict) else {}
            info.update(name=clean_name(_person(user)), username=clean_name(user.get('username') or '', 40), id=_number(user.get('id')), bot=bool(user.get('is_bot')))
        elif kind == 'hidden_user':
            info['name'] = clean_name(raw.get('sender_user_name') or '')
        elif kind == 'chat':
            _from_chat(info, raw.get('sender_chat'))
            info['signature'] = clean_name(raw.get('author_signature') or '', 40)
        elif kind == 'channel':
            _from_chat(info, raw.get('chat'))
            info.update(message_id=_number(raw.get('message_id')), signature=clean_name(raw.get('author_signature') or '', 40),
                        link=link(raw.get('chat'), raw.get('message_id')))
        else:
            info['name'] = ''
    elif any(msg.get(key) for key in LEGACY[1:]):
        if isinstance(msg.get('forward_from_chat'), dict):
            chat = msg['forward_from_chat']
            info = {'kind': 'channel' if chat.get('type') == 'channel' else 'chat', 'date': msg.get('forward_date')}
            _from_chat(info, chat)
            info.update(message_id=_number(msg.get('forward_from_message_id')), signature=clean_name(msg.get('forward_signature') or '', 40),
                        link=link(chat, msg.get('forward_from_message_id')))
        elif isinstance(msg.get('forward_from'), dict):
            user = msg['forward_from']
            info = {'kind': 'user', 'date': msg.get('forward_date'), 'name': clean_name(_person(user)), 'username': clean_name(user.get('username') or '', 40),
                    'id': _number(user.get('id')), 'bot': bool(user.get('is_bot'))}
        else:
            info = {'kind': 'hidden_user', 'date': msg.get('forward_date'), 'name': clean_name(msg.get('forward_sender_name') or '')}
    elif msg.get('is_automatic_forward') and isinstance(msg.get('sender_chat'), dict):
        info = {'kind': 'channel', 'date': msg.get('date')}
        _from_chat(info, msg['sender_chat'])
    if info is None:
        return None
    if msg.get('is_automatic_forward'):
        info['automatic'] = True
    if not isinstance(info.get('date'), (int, float)) or isinstance(info.get('date'), bool):
        info['date'] = None
    if info['kind'] == 'user' and info.get('id') is not None:
        if owner_id is not None and info['id'] == owner_id:
            info['own'] = True
        elif bot_id is not None and info['id'] == bot_id:
            info['this_bot'] = True
    return info


def forwarded(msg):
    return bool(isinstance(msg, dict) and (any(msg.get(key) for key in LEGACY) or msg.get('is_automatic_forward')))


def when(ts, now=None):
    """«пт 21.11 14:03»; год — если не текущий. Пусто, если даты нет."""
    try:
        value, current = local(ts), local(now)
    except (OSError, OverflowError, ValueError, TypeError):
        return ''
    return DAYS[value.weekday()] + value.strftime(' %d.%m %H:%M' if value.year == current.year else ' %d.%m.%Y %H:%M')


def _clock(ts):
    try:
        return local(ts).strftime('%H:%M')
    except (OSError, OverflowError, ValueError, TypeError):
        return ''


def _day(ts):
    try:
        return local(ts).date()
    except (OSError, OverflowError, ValueError, TypeError):
        return None


def short(info):
    """Короткое имя автора для строки пачки."""
    if not info:
        return 'неизвестно кто'
    if info.get('own'):
        return 'хозяин'
    if info.get('this_bot'):
        return 'этот бот'
    name = info.get('name') or ('@' + info['username'] if info.get('username') else '')
    if info['kind'] == 'channel':
        return 'канал «' + (name or 'без названия') + '»'
    if info['kind'] == 'chat':
        return 'чат «' + (name or 'без названия') + '»'
    if not name:
        name = 'id ' + str(info['id']) if info.get('id') is not None else 'неизвестно кто'
    return name + (' (бот)' if info.get('bot') else '')


def describe(info):
    """Кто автор — одной фразой («от: …», «из канала …»)."""
    if info.get('own'):
        return 'от: самого хозяина (его прежнее сообщение)'
    if info.get('this_bot'):
        return 'от: этого бота (твоё прежнее сообщение)'
    kind = info['kind']
    username = ' @' + info['username'] if info.get('username') else ''
    if kind == 'user':
        name = info.get('name') or ('id ' + str(info['id']) if info.get('id') is not None else 'неизвестно кто')
        return 'от: ' + name + username + (' (бот)' if info.get('bot') else '')
    if kind == 'hidden_user':
        return 'от: ' + (info.get('name') or 'неизвестно кто') + ' (скрыл профиль)'
    signature = ' · подпись «' + info['signature'] + '»' if info.get('signature') else ''
    if kind == 'chat':
        return 'от имени чата «' + (info.get('name') or 'без названия') + '»' + username + signature
    if kind == 'channel':
        post = ' · пост ' + info['link'] if info.get('link') else ' · пост ' + str(info['message_id']) if info.get('message_id') else ''
        return ('автоматически ' if info.get('automatic') else '') + 'из канала «' + (info.get('name') or 'без названия') + '»' + username + post + signature
    return 'от: ' + (info.get('name') or 'неизвестно кто')


def _author_key(info):
    return (info.get('kind'), info.get('id'), info.get('name'), bool(info.get('own')), bool(info.get('this_bot')))


def source(origins, now=None):
    """Строка источника рамки: кто и когда написал. Чужие имена уже очищены и укорочены."""
    origins = [o for o in origins or [] if o]
    if not origins:
        return ''
    authors = []
    for info in origins:
        if all(_author_key(info) != _author_key(seen) for seen in authors):
            authors.append(info)
    if len(authors) == 1:
        parts = [describe(authors[0])]
    else:
        parts = ['от: ' + ', '.join(short(info) for info in authors[:3]) + (' и ещё ' + str(len(authors) - 3) if len(authors) > 3 else '')]
    dates = [o['date'] for o in origins if o.get('date') is not None]
    if dates:
        first, last = min(dates), max(dates)
        stamp = when(first, now)
        if stamp and last != first:
            stamp += '–' + (_clock(last) if _day(first) == _day(last) else when(last, now))
        if stamp:
            parts.append('написано ' + stamp)
    return ' · '.join(parts)


def media(msg):
    """Отпечаток вложений сообщения: вид, file_unique_id (или file_id) и имя файла — чтобы заметить замену."""
    out = []
    for kind in MEDIA:
        info = msg.get(kind) if isinstance(msg, dict) else None
        if isinstance(info, list):
            info = info[-1] if info else None
        if not isinstance(info, dict) or kind == 'document' and msg.get('animation'):
            continue
        item = {'kind': kind, 'id': str(info.get('file_unique_id') or info.get('file_id') or '')}
        if info.get('file_name'):
            item['name'] = clean_name(info['file_name'], 120)
        out.append(item)
    return out


def media_word(msg):
    """«фото», «документ "x.pdf"» — что приложено, без содержимого."""
    for item in media(msg):
        return WORDS[item['kind']] + (' «' + item['name'] + '»' if item.get('name') else '')
    for kind in ('location', 'venue', 'contact', 'poll', 'dice'):
        if isinstance(msg, dict) and msg.get(kind):
            question = (msg.get('poll') or {}).get('question') if kind == 'poll' else None
            return WORDS[kind] + (' «' + clean_name(question, 80) + '»' if isinstance(question, str) and question else '')
    return ''


def media_change(old, new):
    """«было фото → стало документ "x.pdf"» по отпечаткам."""
    def words(items):
        return ', '.join(WORDS.get(i.get('kind'), str(i.get('kind'))) + (' «' + i['name'] + '»' if i.get('name') else '') for i in items) or 'без вложения'
    if [i.get('kind') for i in old] == [i.get('kind') for i in new] and [i.get('name') for i in old] == [i.get('name') for i in new]:
        return 'заменил вложение (' + words(new) + ') другим файлом'
    return 'заменил вложение: было ' + words(old) + ' → стало ' + words(new)


def frame(items, now=None, *, extra='', comment=None):
    """Одна недоверенная рамка на пачку пересланного. items — [{'origin', 'text', 'id', 'date'}]:
    у одного сообщения автор и дата — в строке источника; у пачки — ещё и в начале каждой строки."""
    from ..tools.web import untrusted
    items = list(items or [])
    ids = [i['id'] for i in items if _number(i.get('id')) is not None]
    if len(items) == 1:
        head = 'пересланное сообщение Telegram'
    else:
        head = 'пересланные сообщения Telegram: ' + str(len(items)) + (' (#' + str(min(ids)) + '–#' + str(max(ids)) + ')' if len(ids) > 1 else '')
    src = source([i.get('origin') for i in items], now)
    header = head + (' · ' + src if src else '') + (' · вместе с сообщением хозяина #' + str(comment) if comment else '')
    if len(items) == 1:
        body = str(items[0].get('text') or '')
    else:
        stamps = [(i.get('origin') or {}).get('date') or i.get('date') for i in items]
        days = {_day(s) for s in stamps if s is not None}
        lines = []
        for item, stamp in zip(items, stamps):
            mark = (_clock(stamp) if len(days) <= 1 else when(stamp, now)) if stamp is not None else ''
            first, _, rest = str(item.get('text') or '').partition('\n')
            lines.append(((mark + ' ') if mark else '') + short(item.get('origin')) + ': ' + first + ('\n' + rest if rest else ''))
        body = ('\n\n' if any('\n' in str(i.get('text') or '') for i in items) else '\n').join(lines)
    if extra:
        body += '\n\n' + extra
    return untrusted(header, body)


def item_text(msg, text=None):
    """Текст одного пересланного: подпись/текст, а без них — что приложено."""
    from .telegram_rich_in import body
    text = body(msg) if text is None else text
    word = media_word(msg)
    if word and text:
        return '[' + word + '] ' + text
    return text or ('[' + word + ']' if word else '[пусто]')


def summary(origins, ids, now=None):
    """Для пульта (meta.forward): вид, имя, ссылка, исходная дата, сколько и какие номера."""
    origins = [o for o in origins if o]
    if not origins:
        return None
    first = origins[0]
    names = {_author_key(o) for o in origins}
    return {'kind': first['kind'] if len(names) == 1 else 'mixed', 'name': short(first) if len(names) == 1 else short(first) + ' и другие',
            'link': first.get('link'), 'date': min((o['date'] for o in origins if o.get('date') is not None), default=None),
            'count': len(ids), 'ids': list(ids)[:100]}


def describe_external(external, now=None, owner_id=None, bot_id=None):
    """Ответ на сообщение из другого чата: строка источника рамки и вид вложения.
    Номер чужого сообщения не похож на [#номер] этого разговора — reply_to по нему не работает."""
    external = external if isinstance(external, dict) else {}
    chat = external.get('chat') if isinstance(external.get('chat'), dict) else {}
    title = clean_name(chat.get('title') or _person(chat) or '')
    username = clean_name(chat.get('username') or '', 40)
    number = _number(external.get('message_id'))
    where = ('сообщение ' + str(number) if number is not None else 'сообщение') + (' в «' + title + '»' if title else ' в @' + username if username else ' из другого чата')
    url = link(chat, number)
    info = origin({'forward_origin': external.get('origin')}, owner_id, bot_id)
    parts = [where] + (['ссылка ' + url] if url else []) + ([source([info], now)] if info else [])
    return ' · '.join(p for p in parts if p), media_word(external)


def from_userbot(forward):
    """Пересланное из помощника аккаунта (Telethon) → тот же вид, что у Bot API. forward — dict или True/False."""
    if not isinstance(forward, dict):
        return None
    kind = 'channel' if forward.get('channel') else 'user' if forward.get('id') is not None else 'hidden_user'
    info = {'kind': kind, 'date': forward.get('date') if isinstance(forward.get('date'), (int, float)) else None,
            'name': clean_name(forward.get('name') or ''), 'username': clean_name(forward.get('username') or '', 40), 'id': _number(forward.get('id'))}
    if kind == 'channel':
        info.update(message_id=_number(forward.get('post')), signature=clean_name(forward.get('signature') or '', 40),
                    link=link({'id': forward.get('id'), 'username': forward.get('username')}, forward.get('post')))
    return info
