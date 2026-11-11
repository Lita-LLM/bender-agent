"""Долгоживущий процесс-помощник одного аккаунта: запросы с ответами и поток событий по строкам JSON."""
from __future__ import annotations

import asyncio
import json
import os
import shlex

from ..core.errors import HumanError
from ..guard import sandbox
from .runtime import HELPER, failed

LINE_LIMIT = 64 * 1024 * 1024
ENV = {'PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'SystemRoot', 'PATHEXT', 'COMSPEC', 'TEMP', 'TMP', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
       'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}


class Refused(HumanError):
    """Отказ помощника или Telegram; code — auth, flood, telegram, not_found, network…"""

    def __init__(self, error):
        self.reason = str(error.get('code') or 'error')
        self.seconds = error.get('seconds')
        super().__init__('userbot', 'Telegram не выполнил действие', str(error.get('message') or 'Причина не названа.')[:800])


class Link:
    def __init__(self, ctx, on_event, on_exit):
        self.ctx = ctx
        self.on_event = on_event
        self.on_exit = on_exit
        self.proc = None
        self.reader = None
        self.errors = None
        self.prepared = None
        self.waiting = {}
        self.next = 0
        self.log = []
        self.closing = False

    def alive(self):
        return self.proc is not None and self.proc.returncode is None

    async def start(self):
        host = self.ctx.host
        command = host.userbot_runtime.command()
        info = sandbox.for_app(host)
        info['write_work'] = False
        info['agent_isolation'] = False
        info.get('limits', {}).pop('RLIMIT_CPU', None)  # процесс живёт днями: лимит процессорного времени убил бы его молча
        info.get('limits', {}).pop('RLIMIT_AS', None)
        self.prepared = sandbox.prepare(shlex.join(command), info=info, work_dir=host.dirs.work, allow_net=True, read_dirs=(host.userbot_runtime.root, HELPER.parent),
                                        temp_root=host.dirs.local, raw_argv=command)
        env = {key: value for key, value in os.environ.items() if key in ENV}
        env.update(self.prepared.env)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['PYTHONIOENCODING'] = 'utf-8'
        try:
            self.proc = await asyncio.create_subprocess_exec(*(self.prepared.argv or command), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                             env=env, cwd=str(host.dirs.local), limit=LINE_LIMIT)
        except OSError as error:
            self.prepared.close()
            raise failed('Помощник не запустился: ' + type(error).__name__ + '. Переустанови Telethon в «Связях».') from None
        self.reader = asyncio.ensure_future(self._read())
        self.errors = asyncio.ensure_future(self._stderr())

    async def _stderr(self):
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            self.log = (self.log + [line.decode('utf-8', 'replace').rstrip()[:500]])[-100:]

    async def _read(self):
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except ValueError:  # строка длиннее предела: поток дальше не разобрать
                    break
                if not line:
                    break
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                if 'event' in data:
                    try:
                        self.on_event(data)
                    except Exception:  # noqa: BLE001 — событие не роняет чтение
                        self.ctx.journal.write('channel', 'Аккаунт Telegram: событие не обработано', data={'event': str(data.get('event'))[:40]})
                    continue
                future = self.waiting.pop(data.get('id'), None)
                if future is not None and not future.done():
                    future.set_result(data)
        finally:
            for future in self.waiting.values():
                if not future.done():
                    future.set_exception(failed('Помощник аккаунта остановился. Повтори действие.'))
            self.waiting.clear()
            if not self.closing:
                code = await self.proc.wait()
                self.on_exit(code, list(self.log[-5:]))

    async def call(self, method, params=None, *, timeout=90):
        if not self.alive():
            raise failed('Аккаунт Telegram сейчас не подключён. Проверь «Связи».')
        self.next += 1
        rid = self.next
        future = asyncio.get_running_loop().create_future()
        self.waiting[rid] = future
        try:
            self.proc.stdin.write((json.dumps({'id': rid, 'method': method, 'params': params or {}}, ensure_ascii=False) + '\n').encode())
            await self.proc.stdin.drain()
            data = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:  # в Python 3.11+ это подкласс OSError: ловим раньше
            raise failed('Telegram не ответил за ' + str(int(timeout)) + ' с. Действие могло выполниться — проверь перед повтором.') from None
        except (ConnectionError, OSError):
            raise failed('Помощник аккаунта остановился. Повтори действие.') from None
        finally:
            self.waiting.pop(rid, None)
        if data.get('error'):
            raise Refused(data['error'])
        result = data.get('result')
        if not isinstance(result, dict):
            raise failed('Помощник вернул неверный ответ.')
        return result

    async def close(self):
        self.closing = True
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.stdin.close()
            except (OSError, RuntimeError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self.reader, self.errors):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.prepared is not None:
            self.prepared.close()
            self.prepared = None
        self.proc = None
