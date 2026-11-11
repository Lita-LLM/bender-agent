"""Отдельная служба голоса после обновлений и выбора голоса.

Обновление перезапускает ядро, но не службу голоса: старый код молча игнорировал выбор голоса.
Поэтому служба сообщает отпечаток своего кода в /health, и при первом обращении ядро сверяет
его со своим local_server.py; устарела — перезапускает её через systemd. Выбранного голоса
нет в пакете — он докачивается в фоне, а пока звучит голос по умолчанию.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

from ..core.errors import HumanError

SERVER = Path(__file__).with_name('local_server.py')
RETRY = 3600  # не скачался — следующая попытка не раньше чем через час


def code():
    return hashlib.sha256(SERVER.read_bytes()).hexdigest()[:16]


async def health(app):
    from .cli import request
    try:
        data = json.loads(await request(app, '/health'))
    except (HumanError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def ensure(app):
    """Один раз за запуск ядра: служба голоса на том же коде, что и ядро."""
    host = app.host
    if getattr(host, '_voice_fresh', False):
        return
    lock = host.__dict__.setdefault('_voice_fresh_lock', asyncio.Lock())
    async with lock:
        if getattr(host, '_voice_fresh', False):
            return
        info = await health(app)
        if info is None:
            return  # служба не отвечает — ошибку покажет сам запрос
        host._voice_fresh = True
        if info.get('code') == code():
            return
        from ..core.service import unit_path, command
        if sys.platform.startswith('linux') and unit_path(voice=True).exists():
            try:
                await asyncio.to_thread(command, ['systemctl', '--user', 'restart', 'bender-voice.service'])
            except HumanError:
                host.journal.write('system', 'Служба голоса на старом коде и не перезапустилась: systemctl --user restart bender-voice.service', actor='system')
                return
            for _ in range(40):
                await asyncio.sleep(0.5)
                info = await health(app)
                if info and info.get('code') == code():
                    break
            host.journal.write('system', 'Служба голоса перезапущена на свежий код после обновления', actor='system')
        else:
            host.journal.write('system', 'Служба голоса работает на старом коде (выбор голоса не действует): перезапусти bender voice serve', actor='system')


def missing(app, voice):
    from .connect import folder
    from .install import VOICES, installed_voices
    place = folder(app)
    return voice in VOICES and (place / 'installed.json').is_file() and voice not in installed_voices(place)


def fetch(app, voice):
    """Докачать голос в фоне; повторный выбор того же голоса не запускает вторую загрузку."""
    host = app.host
    busy = host.__dict__.setdefault('_voice_fetching', {})
    failed = host.__dict__.setdefault('_voice_failed', {})
    if voice in busy and not busy[voice].done() or time.monotonic() - failed.get(voice, -RETRY) < RETRY:
        return  # уже качается или недавно не скачался: не повторять на каждой фразе
    from .install import add_voice
    from ..core.settings import PIPER_VOICES
    name = PIPER_VOICES.get(voice, voice).split(' — ')[0]

    async def run():
        try:
            await add_voice(app, voice)
            text = 'Голос «' + name + '» скачан и уже звучит.'
        except HumanError as exc:
            failed[voice] = time.monotonic()
            text = 'Голос «' + name + '» не скачался: ' + exc.title + '. ' + exc.hint
        except Exception as exc:  # noqa: BLE001 — итог хозяину, а не тихая смерть задачи
            failed[voice] = time.monotonic()
            text = 'Голос «' + name + '» не скачался: ' + str(exc)[:200]
        host.journal.write('system', text, actor='system', data={'voice': voice})
        host.bus.publish({'type': 'notify', 'kind': 'voice', 'agent_id': '*', 'text': text})

    host.journal.write('system', 'Скачиваю голос «' + name + '»; пока звучит голос по умолчанию', actor='system', data={'voice': voice})
    busy[voice] = asyncio.ensure_future(run())
