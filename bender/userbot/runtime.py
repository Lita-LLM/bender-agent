"""Telethon живёт в собственной папке (venv) внутри данных Бендера: ставится кнопкой хозяина, удаляется вместе с папкой.

Системный Python и остальной Бендер его не видят — конфликтов версий нет. Ядро Telethon не импортирует никогда.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from ..core.errors import HumanError

REQUIREMENT = 'telethon>=1.40,<2'
HELPER = Path(__file__).with_name('helper.py')


def failed(hint, title='Аккаунт Telegram недоступен'):
    return HumanError('userbot', title, hint)


class Runtime:
    """Одна установка на весь Бендер; аккаунтов (агентов) может быть несколько."""

    def __init__(self, host):
        self.host = host
        self.root = host.dirs.local / 'userbot-runtime'
        self.busy = False
        self.error = ''

    def python(self) -> Path:
        return self.root / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')

    def marker(self) -> dict:
        try:
            data = json.loads((self.root / 'bender-userbot.json').read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def stand_in():
        """Проверка без сети и без Telethon (tests/ui, полигон): путь к фальшивому помощнику в окружении самого Бендера."""
        return os.environ.get('BENDER_USERBOT_HELPER') or ''

    def installed(self) -> bool:
        return bool(self.stand_in()) or (bool(self.marker().get('telethon')) and self.python().exists())

    def public(self) -> dict:
        return {'installed': self.installed(), 'version': self.marker().get('telethon', '') or ('проверочный' if self.stand_in() else ''), 'busy': self.busy, 'error': self.error,
                'path': str(self.root), 'requirement': REQUIREMENT}

    def command(self) -> list[str]:
        """Команда помощника. Тесты подменяют её фальшивым помощником."""
        if self.stand_in():
            return [sys.executable, self.stand_in()]
        if not self.installed():
            raise failed('Сначала установи Telethon: Связи → Аккаунт Telegram → «Установить».')
        return [str(self.python()), '-I', str(HELPER)]

    async def _run(self, argv, timeout):
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise failed('Установка не уложилась во время. Проверь интернет и повтори.', 'Telethon не установлен') from None
        text = out.decode('utf-8', 'replace')
        if proc.returncode:
            raise failed(self.host.guard.scrub(text.strip()[-600:]) or 'Команда завершилась с ошибкой.', 'Telethon не установлен')
        return text

    async def install(self) -> dict:
        """Только хозяин (API пульта). Ставит в свою папку; при любой ошибке папка убирается целиком."""
        if self.busy:
            raise failed('Установка уже идёт.')
        from ..tools.registry import ToolContext
        await ToolContext(self.host, actor='owner', tool='userbot_install').check('settings', 'userbot', 'установить Telethon в отдельную папку Бендера', detail={'path': str(self.root), 'package': REQUIREMENT})
        self.busy = True
        self.error = ''
        self.event()
        try:
            if self.root.exists():
                shutil.rmtree(self.root)
            try:
                await self._run([sys.executable, '-m', 'venv', str(self.root)], 180)
            except HumanError as error:
                raise failed('Python не смог создать отдельное окружение (venv). В Debian/Ubuntu поставь пакет python3-venv и повтори. ' + error.hint, 'Telethon не установлен') from None
            await self._run([str(self.python()), '-m', 'pip', 'install', '--no-input', '--disable-pip-version-check', '--no-cache-dir', REQUIREMENT], 900)
            version = (await self._run([str(self.python()), '-I', '-c', 'import telethon;print(telethon.__version__)'], 60)).strip().splitlines()[-1]
            (self.root / 'bender-userbot.json').write_text(json.dumps({'telethon': version, 'installed_at': time.time()}), encoding='utf-8')
            self.host.journal.write('channel', 'Telethon ' + version + ' установлен для аккаунтов Telegram', actor='owner', data={'path': str(self.root)})
        except BaseException as error:
            shutil.rmtree(self.root, ignore_errors=True)
            self.error = (error.title + '. ' + error.hint) if isinstance(error, HumanError) else 'Установка прервана.'
            raise
        finally:
            self.busy = False
            self.event()
        return self.public()

    async def remove(self) -> dict:
        """Останавливает все аккаунты и стирает папку. Входы (сессии в Сейфе) остаются — их убирает «Выйти из аккаунта»."""
        if self.busy:
            raise failed('Сначала дождись конца установки.')
        for ctx in {id(c): c for c in [self.host, *self.host.agents.loaded.values()]}.values():
            bot = getattr(ctx, '_userbot', None)
            if bot is not None:
                await bot.stop()
        shutil.rmtree(self.root, ignore_errors=True)
        if self.root.exists():
            raise failed('Папку не удалось стереть целиком: ' + str(self.root) + '. Удали её вручную.', 'Telethon не удалён')
        self.error = ''
        self.host.journal.write('channel', 'Telethon удалён; аккаунты Telegram остановлены', actor='owner')
        self.event()
        return self.public()

    def event(self):
        self.host.bus.publish({'type': 'channel', 'channel': 'userbot', 'agent_id': '*'})
