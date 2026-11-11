"""Руки на экране Windows (превью, выключено): агент видит и трогает только разрешённые хозяином окна.

Окно узнаётся по программе (путь exe), а не по заголовку: заголовок любая программа ставит какой хочет.
Жёсткий запрет, который профилем не снимается: пульт и браузер с ним, терминалы, окно «Выполнить», Telegram, менеджеры паролей,
диспетчер задач, реестр, консоль управления. Перед каждым действием окно ищется заново, ввод идёт только в окно на переднем плане.
Каждое действие — карточка Предохранителя (хозяин может ответить «в этом разговоре»). Исполнитель — отдельный процесс (screen_exec.py).
Только Windows; на других системах тулы честно отвечают, что недоступны. Проверка — приёмка на настоящем Windows."""
from __future__ import annotations

import asyncio
import base64
import json
import ntpath
import sys
from pathlib import Path

from ..core.errors import HumanError
from .registry import ToolContext, ToolResult

HARD_PROGRAMS = {'bender.exe', 'cmd.exe', 'powershell.exe', 'pwsh.exe', 'wt.exe', 'windowsterminal.exe', 'conhost.exe', 'openconsole.exe', 'telegram.exe',
                 'keepass.exe', 'keepassxc.exe', '1password.exe', 'bitwarden.exe', 'lastpass.exe', 'dashlane.exe', 'taskmgr.exe', 'regedit.exe', 'mmc.exe',
                 'python.exe', 'pythonw.exe', 'py.exe'}
HARD_TITLES = ('бендер', 'bender', 'выполнить', 'run', 'диспетчер задач', 'task manager')
EXECUTOR = Path(__file__).with_name('screen_exec.py')


def program(window) -> str:
    return ntpath.basename(str(window.get('exe') or '')).lower()


def verdict(window, allowed) -> tuple[bool, str]:
    """(можно ли, почему нет). Жёсткий список проверяется первым и от настроек не зависит."""
    name = program(window)
    if not name:
        return False, 'программа окна неизвестна'
    if name in HARD_PROGRAMS:
        return False, f'{name} закрыта всегда (терминалы, Telegram, менеджеры паролей, системные средства)'
    title = str(window.get('title') or '').lower()
    if any(word in title for word in HARD_TITLES):
        return False, 'в заголовке пульт, окно «Выполнить» или диспетчер: закрыто всегда'
    if str(window.get('class') or '') == '#32770' and name == 'explorer.exe':
        return False, 'системные диалоги проводника закрыты'
    if name not in {str(a).lower() for a in allowed}:
        return False, f'{name} не в списке разрешённых программ (Настройки → Окна Windows)'
    return True, ''


class Executor:
    def __init__(self):
        self.proc = None
        self.lock = asyncio.Lock()

    async def call(self, **command):
        async with self.lock:
            if self.proc is None or self.proc.returncode is not None:
                self.proc = await asyncio.create_subprocess_exec(sys.executable, '-I', str(EXECUTOR), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                                 stderr=asyncio.subprocess.DEVNULL)
            self.proc.stdin.write(json.dumps(command).encode() + b'\n')
            await self.proc.stdin.drain()
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), 30)
            except asyncio.TimeoutError:
                self.proc.kill()
                raise HumanError('screen_timeout', 'Исполнитель окон не ответил', 'Повтори; процесс перезапущен.') from None
        if not line:
            raise HumanError('screen_dead', 'Исполнитель окон завершился', 'Повтори действие.')
        reply = json.loads(line)
        if not reply.get('ok'):
            raise HumanError('screen_error', 'Окно не выполнило команду', str(reply.get('error')))
        return reply


def executor_of(ctx) -> Executor:
    host = ctx.app.host if hasattr(ctx.app, 'host') else ctx.app
    if getattr(host, '_screen', None) is None:
        host._screen = Executor()
    return host._screen


def require(ctx):
    cfg = ctx.app.settings.group('screen')
    if not cfg['enabled']:
        raise HumanError('screen_off', 'Работа с окнами выключена', 'Включи в Настройки → Окна Windows (превью) и укажи разрешённые программы.')
    if sys.platform != 'win32':
        raise HumanError('screen_platform', 'Окна пока только на Windows', 'На этой системе тулы недоступны.')
    return cfg


async def allowed_window(ctx, executor, hwnd, cfg):
    windows = (await executor.call(op='list'))['windows']
    window = next((w for w in windows if w['hwnd'] == int(hwnd)), None)
    if window is None:
        raise HumanError('screen_gone', 'Такого окна больше нет', 'Вызови screen_windows заново.')
    ok, why = verdict(window, cfg['programs'])
    if not ok:
        raise HumanError('screen_denied', 'Это окно трогать нельзя', why)
    return window


def register(reg) -> None:
    async def guarded(make):
        try:
            return await make()
        except HumanError as exc:
            if exc.code == 'denied':
                raise
            return ToolResult(exc.title + '. ' + exc.hint, ok=False, summary=exc.title)

    @reg.tool('screen_windows', 'Разрешённые окна', group='screen')
    async def screen_windows(ctx: ToolContext) -> ToolResult:
        """Список окон, которые хозяин разрешил агенту: номер, программа, заголовок."""
        async def make():
            cfg = require(ctx)
            await ctx.check('screen.look', 'windows', 'узнать, какие разрешённые окна открыты')
            rows = [w for w in (await executor_of(ctx).call(op='list'))['windows'] if verdict(w, cfg['programs'])[0]]
            from .web import untrusted
            text = '\n'.join(f"{w['hwnd']}: {program(w)} — {w['title'][:80]} ({w['width']}x{w['height']})" for w in rows) or 'Разрешённых окон сейчас нет.'
            return ToolResult(untrusted('заголовки окон', text), summary=f'окон: {len(rows)}', tainted=True)
        return await guarded(make)

    @reg.tool('screen_look', 'Посмотреть окно', group='screen')
    async def screen_look(ctx: ToolContext, window: int) -> ToolResult:
        """Снимок разрешённого окна (картинка). Координаты для screen_click считаются от левого верхнего угла этого снимка.

        window — номер окна из screen_windows
        """
        async def make():
            cfg = require(ctx)
            ex = executor_of(ctx)
            info = await allowed_window(ctx, ex, window, cfg)
            await ctx.check('screen.look', program(info), f'посмотреть окно {program(info)}')
            shot = await ex.call(op='shot', hwnd=int(window))
            parts = [{'type': 'text', 'text': '[Снимок окна, недоверенные данные]'},
                     {'type': 'image', 'media_type': 'image/png', 'data': shot['png'], 'path': '', 'source': 'screen_look'}]
            ctx.tainted = True
            return ToolResult(f"Снимок окна {program(info)} {shot['width']}x{shot['height']} приложен картинкой после результатов тулов.", summary='снимок окна', tainted=True, model_parts=parts)
        return await guarded(make)

    @reg.tool('screen_click', 'Нажать в окне', group='screen')
    async def screen_click(ctx: ToolContext, window: int, x: int, y: int, button: str = 'left') -> ToolResult:
        """Нажать в точке разрешённого окна (координаты снимка screen_look).

        window — номер окна
        x — слева, пикселей
        y — сверху, пикселей
        button — left или right
        """
        async def make():
            cfg = require(ctx)
            ex = executor_of(ctx)
            info = await allowed_window(ctx, ex, window, cfg)
            await ctx.check('screen.act', program(info), f'нажать в окне {program(info)} ({int(x)}, {int(y)})')
            await allowed_window(ctx, ex, window, cfg)  # пока хозяин думал над карточкой, окно могло смениться
            await ex.call(op='click', hwnd=int(window), x=int(x), y=int(y), button='right' if button == 'right' else 'left')
            return ToolResult('Нажато. Посмотри окно, чтобы увидеть результат.', summary='нажато')
        return await guarded(make)

    @reg.tool('screen_type', 'Печатать в окне', group='screen')
    async def screen_type(ctx: ToolContext, window: int, text: str = '', key: str = '') -> ToolResult:
        """Напечатать текст или нажать клавишу (enter, tab, escape, backspace, delete, стрелки up/down/left/right) в разрешённом окне, предварительно выведя его вперёд.

        window — номер окна
        text — что напечатать (до 500 знаков)
        key — одна из клавиш вместо текста
        """
        async def make():
            cfg = require(ctx)
            ex = executor_of(ctx)
            info = await allowed_window(ctx, ex, window, cfg)
            if key and key not in ('enter', 'tab', 'escape', 'backspace', 'delete', 'up', 'down', 'left', 'right'):
                return ToolResult('Неизвестная клавиша.', ok=False, summary='клавиша')
            if not key and not text:
                return ToolResult('Нужен текст или клавиша.', ok=False, summary='пусто')
            await ctx.check('screen.act', program(info), f'печатать в окне {program(info)}', detail={'text': text[:200], 'key': key})
            await allowed_window(ctx, ex, window, cfg)
            await ex.call(op='key', hwnd=int(window), key=key) if key else await ex.call(op='type', hwnd=int(window), text=text)
            return ToolResult('Введено.', summary='введено')
        return await guarded(make)
