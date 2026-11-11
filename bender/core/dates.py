"""Местные даты для модели; без зависимости от языка локали ОС."""
import datetime as dt
import math
import re

DAYS = ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')
PREFIX = re.compile(r'^\[(?:пн|вт|ср|чт|пт|сб|вс) \d{2}\.\d{2} \d{2}:\d{2}\](?: \(прошло \d+ (?:мин|ч|дн)\))?$')
# Метки, которые система ставит перед сообщениями истории; модель иногда начинает с них свой ответ.
LABELS = re.compile(r'^(?:\s*(?:\[(?:пн|вт|ср|чт|пт|сб|вс) \d{2}\.\d{2} \d{2}:\d{2}\](?: \(прошло \d+ (?:мин|ч|дн)\))?|\[#\d+\]))+[ \t]*\n?')


def local(ts=None):
    return dt.datetime.fromtimestamp(ts).astimezone() if ts is not None else dt.datetime.now().astimezone()


def stamp(ts):
    try: value = local(ts)
    except (OSError, OverflowError, ValueError, TypeError): return '[время не задано]'
    return '[' + DAYS[value.weekday()] + value.strftime(' %d.%m %H:%M]')


def prefix(ts, previous=None):
    text = stamp(ts)
    try: gap = ts - previous if previous is not None else 0
    except TypeError: gap = 0
    if math.isfinite(gap) and gap > 1800:
        amount, unit = (int(gap // 86400), 'дн') if gap >= 86400 else (int(gap // 3600), 'ч') if gap >= 3600 else (int(gap // 60), 'мин')
        text += f' (прошло {amount} {unit})'
    return text


def message_prefix(row):
    # Время — только у реплик хозяина: в своих ответах модель стала бы его повторять.
    if row.get('role') != 'user' or row.get('created_at') is None: return ''
    saved = (row.get('meta') or {}).get('time_prefix')
    return saved if isinstance(saved, str) and PREFIX.fullmatch(saved) else prefix(row['created_at'])


def strip_labels(text):
    """Срезать скопированные моделью метки «[пн 26.10 17:11] [#10126]» в начале её ответа."""
    if not isinstance(text, str): return text
    match = LABELS.match(text)
    return text[match.end():] if match and match.end() and text[match.end():].strip() else text


def content(row):
    value = row.get('content')
    if row.get('role') == 'assistant': value = strip_labels(value)
    label = message_prefix(row)
    number=(row.get('meta') or {}).get('telegram_message_id')
    if isinstance(number,int):label += (' ' if label else '') + f'[#{number}]'
    if not label: return value
    if isinstance(value, list): return [{'type': 'text', 'text': label + '\n'}, *value]
    return label + '\n' + str(value or '')


def current():
    value = local()
    return {'date_time': value.strftime('%d.%m.%Y %H:%M'), 'timezone': str(value.tzinfo)}


def without_prefix(text):
    """Служебная дата не должна становиться ключевыми словами поиска/намерений."""
    first, sep, rest = text.partition('\n')
    return rest if sep and (PREFIX.fullmatch(first) or first == '[время не задано]') else text


def calendar(ts):
    try: return local(ts).strftime('%d.%m.%Y %H:%M')
    except (OSError, OverflowError, ValueError, TypeError): return 'дата не задана'
