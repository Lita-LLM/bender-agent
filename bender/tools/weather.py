"""Тул погоды: Open-Meteo, бесплатно и без ключа (некоммерческое использование).

Ответ собирается из чисел и названия места, а не из чужого текста, поэтому ход не заражается:
название места очищается до букв, цифр, пробелов и дефисов.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode

from ..net.http_client import NetError, Timeouts
from .registry import ToolContext, ToolResult

GEO = 'https://geocoding-api.open-meteo.com/v1/search'
FORECAST = 'https://api.open-meteo.com/v1/forecast'

# коды погоды WMO
CODES = {0: 'ясно', 1: 'почти ясно', 2: 'переменная облачность', 3: 'пасмурно', 45: 'туман', 48: 'изморозь и туман',
         51: 'слабая морось', 53: 'морось', 55: 'сильная морось', 56: 'ледяная морось', 57: 'сильная ледяная морось',
         61: 'небольшой дождь', 63: 'дождь', 65: 'сильный дождь', 66: 'ледяной дождь', 67: 'сильный ледяной дождь',
         71: 'небольшой снег', 73: 'снег', 75: 'сильный снег', 77: 'снежная крупа',
         80: 'небольшой ливень', 81: 'ливень', 82: 'сильный ливень', 85: 'снегопад', 86: 'сильный снегопад',
         95: 'гроза', 96: 'гроза с градом', 99: 'сильная гроза с градом'}
DAYS = ('сегодня', 'завтра', 'послезавтра')


def plain(value, limit=60):
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s\-’\'.,]', '', str(value or '')))[:limit].strip()


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def deg(value):
    value = number(value)
    return '—' if value is None else f'{round(value):+d}°C'.replace('+0', '0')


async def get_json(ctx, url, purpose):
    await ctx.check('net.read', url, purpose)
    try:
        resp = await ctx.app.http.get(url, headers={'Accept': 'application/json'}, timeouts=Timeouts(connect=10, read=15, total=25))
    except NetError as e:
        raise LookupError('Сервис погоды недоступен: ' + str(e)[:200]) from None
    async with resp:
        raw = await resp.read(limit=512 * 1024)
    if resp.status >= 400:
        raise LookupError(f'Сервис погоды ответил {resp.status}.')
    try:
        data = json.loads(raw)
    except ValueError:
        raise LookupError('Сервис погоды прислал не JSON.') from None
    if not isinstance(data, dict):
        raise LookupError('Сервис погоды прислал непонятный ответ.')
    return data


def register(reg) -> None:
    @reg.tool('weather', 'Погода', group='web')
    async def weather(ctx: ToolContext, place: str, days: int = 1) -> ToolResult:
        """Погода сейчас и прогноз по дням (Open-Meteo, без ключа). Хозяин не назвал место — возьми его город из памяти или спроси.

        place — город или населённый пункт, лучше по-русски («Казань», «Сочи»); можно «Город, страна»
        days — дней прогноза от 1 до 7: 1 — сегодня, 2 — с завтра
        """
        place = str(place or '').strip()
        if not place:
            return ToolResult('Нужно место: город или населённый пункт.', ok=False, summary='нет места')
        days = max(1, min(7, int(days or 1)))
        name, _, country = place.partition(',')
        try:
            found = await get_json(ctx, GEO + '?' + urlencode({'name': name.strip()[:100], 'count': 5, 'language': 'ru', 'format': 'json'}), 'найти место для погоды: ' + place[:80])
            spots = [r for r in found.get('results') or [] if isinstance(r, dict) and number(r.get('latitude')) is not None and number(r.get('longitude')) is not None]
            if country.strip():
                spots = [r for r in spots if country.strip().lower() in (str(r.get('country') or '') + ' ' + str(r.get('admin1') or '')).lower()] or spots
            if not spots:
                return ToolResult(f'Место «{plain(place)}» не найдено. Уточни название.', ok=False, summary='место не найдено')
            spot = spots[0]
            query = {'latitude': spot['latitude'], 'longitude': spot['longitude'], 'timezone': 'auto', 'forecast_days': days, 'wind_speed_unit': 'ms',
                     'current': 'temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m,wind_gusts_10m',
                     'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max'}
            data = await get_json(ctx, FORECAST + '?' + urlencode(query), 'узнать погоду: ' + plain(spot.get('name')))
        except LookupError as exc:
            return ToolResult(str(exc), ok=False, summary='погода недоступна')
        where = ', '.join(p for p in (plain(spot.get('name')), plain(spot.get('admin1')), plain(spot.get('country'))) if p)
        lines = [where + (' (местное время ' + plain(data['current'].get('time'), 20).replace('T', ' ') + ')' if isinstance(data.get('current'), dict) else '')]
        now = data.get('current') if isinstance(data.get('current'), dict) else {}
        if now:
            parts = ['сейчас ' + deg(now.get('temperature_2m')), 'ощущается ' + deg(now.get('apparent_temperature')),
                     CODES.get(now.get('weather_code'), '')]
            if number(now.get('wind_speed_10m')) is not None:
                parts.append(f"ветер {round(now['wind_speed_10m'])} м/с" + (f", порывы {round(now['wind_gusts_10m'])}" if number(now.get('wind_gusts_10m')) else ''))
            if number(now.get('relative_humidity_2m')) is not None:
                parts.append(f"влажность {round(now['relative_humidity_2m'])}%")
            if number(now.get('precipitation')):
                parts.append(f"осадки {now['precipitation']} мм")
            lines.append(', '.join(p for p in parts if p))
        daily = data.get('daily') if isinstance(data.get('daily'), dict) else {}
        dates = daily.get('time') if isinstance(daily.get('time'), list) else []

        def at(key, i):
            values = daily.get(key)
            return values[i] if isinstance(values, list) and i < len(values) else None

        for i, date in enumerate(dates[:days]):
            label = (DAYS[i] + ' ' if i < len(DAYS) else '') + plain(date, 10)
            parts = [f'{deg(at("temperature_2m_min", i))}…{deg(at("temperature_2m_max", i))}', CODES.get(at('weather_code', i), '')]
            if number(at('precipitation_probability_max', i)) is not None:
                parts.append(f'вероятность осадков {round(at("precipitation_probability_max", i))}%')
            if number(at('precipitation_sum', i)):
                parts.append(f'осадки {at("precipitation_sum", i)} мм')
            if number(at('wind_speed_10m_max', i)) is not None:
                parts.append(f'ветер до {round(at("wind_speed_10m_max", i))} м/с')
            lines.append(label + ': ' + ', '.join(p for p in parts if p))
        if len(lines) == 1:
            return ToolResult('Сервис погоды не прислал данных для ' + where + '.', ok=False, summary='нет данных')
        other = [plain(r.get('name')) + ', ' + plain(r.get('admin1') or r.get('country')) for r in spots[1:3]]
        if other:
            lines.append('Если место не то, есть ещё: ' + '; '.join(other) + ' — уточни «Город, область».')
        lines.append('Данные Open-Meteo.')
        return ToolResult('\n'.join(lines), summary='погода: ' + plain(spot.get('name'), 40),
                          data={'place': where, 'latitude': spot['latitude'], 'longitude': spot['longitude'], 'current': now, 'daily': daily})
