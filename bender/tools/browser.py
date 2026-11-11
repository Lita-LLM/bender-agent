"""Браузер агента (превью): уже установленный Chrome/Edge/Chromium, управляемый по каналу DevTools без открытого порта.

Окно — настоящее (по умолчанию без окна, headless; в настройках можно показать). Профиль отдельный: data/local/browser.
Страница отдаётся модели текстом с пронумерованными элементами («[12] кнопка «Войти»»); всё, что пришло со страницы,
недоверенное. Адреса — через Предохранитель (net.read), ввод и нажатия кнопок — net.send. Схемы file: и chrome: и порт
самого пульта закрыты. Выключен по умолчанию. Проверено на Linux; Windows (канал через дескрипторы) и Termux — не проверялись."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

from ..core.errors import HumanError
from .registry import ToolContext, ToolResult
from .web import untrusted

CANDIDATES = ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'microsoft-edge', 'microsoft-edge-stable', 'chrome', 'msedge')
SNAPSHOT = r"""(() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 90);
  document.querySelectorAll('[data-bref]').forEach(e => e.removeAttribute('data-bref'));
  const items = [];
  const sel = 'a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[onclick]';
  for (const el of document.querySelectorAll(sel)) {
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height || items.length >= 80) continue;
    const n = items.length + 1;
    el.setAttribute('data-bref', n);
    const tag = el.tagName.toLowerCase();
    const kind = tag === 'a' ? 'ссылка' : tag === 'button' || el.getAttribute('role') === 'button' ? 'кнопка' : tag === 'select' ? 'список' : tag === 'textarea' ? 'поле' : tag === 'input' ? ((el.type || 'text') === 'checkbox' ? 'галочка' : ['submit', 'button'].includes(el.type) ? 'кнопка' : 'поле') : 'элемент';
    const label = clean(el.innerText || el.value || el.getAttribute('aria-label') || el.placeholder || el.title || el.name || el.getAttribute('href'));
    items.push('[' + n + '] ' + kind + ' «' + label + '»' + (tag === 'a' ? ' → ' + clean(el.href) : ''));
  }
  return JSON.stringify({title: document.title, url: location.href, text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 6000), items});
})()"""


def find_browser() -> str | None:
    forced = os.environ.get('BENDER_BROWSER')
    if forced and Path(forced).exists():
        return forced
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for path in ('/opt/pw-browsers/chromium', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe', r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                 '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'):
        if Path(path).exists():
            return path
    return None


def check_url(ctx, url: str) -> str:
    parsed = urlparse(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise HumanError('browser_url', 'Браузер открывает только адреса http(s)', 'Схемы file:, chrome: и подобные закрыты.')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    host = ctx.app.host if hasattr(ctx.app, 'host') else ctx.app
    own = host.store.meta_get('web:active_port') or host.settings.get('web', 'port', 8770)
    if parsed.hostname in ('127.0.0.1', 'localhost', '::1') and port == int(own):
        raise HumanError('browser_url', 'Пульт Бендера браузеру агента закрыт', 'Агент не должен управлять собственным пультом через страницу.')
    return url


class Chrome:
    def __init__(self, exe: str, profile: Path, headless: bool):
        self.exe, self.profile, self.headless = exe, profile, headless
        self.proc = None
        self.ids = 0
        self.waiting: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self.session = None
        self.write_fd = None
        self.loop = None

    async def start(self):
        if os.name!='posix':
            raise HumanError('browser_platform','Этот канал браузера пока недоступен в Windows','Используй внешний браузерный MCP; встроенный канал пока проверен только на POSIX.')
        import fcntl
        self.loop = asyncio.get_running_loop()
        self.profile.mkdir(parents=True, exist_ok=True)
        cmd_r, cmd_w = os.pipe()
        res_r, res_w = os.pipe()
        cmd_r, res_w = (fcntl.fcntl(fd, fcntl.F_DUPFD, 10) for fd in (cmd_r, res_w))

        def child():
            os.dup2(cmd_r, 3)
            os.dup2(res_w, 4)
        args = [self.exe, '--remote-debugging-pipe', f'--user-data-dir={self.profile}', '--no-first-run', '--no-default-browser-check', '--disable-extensions']
        if self.headless:
            args.append('--headless=new')
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            args.append('--no-sandbox')
        args.append('about:blank')
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=False, preexec_fn=child)
        except OSError as exc:
            raise HumanError('browser_start', 'Браузер не запустился', str(exc)[:200]) from None
        finally:
            os.close(cmd_r)
            os.close(res_w)
        self.write_fd = cmd_w
        threading.Thread(target=self._read, args=(res_r,), daemon=True, name='bender-browser-pipe').start()
        target = await self.call('Target.createTarget', {'url': 'about:blank'})
        attached = await self.call('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})
        self.session = attached['sessionId']
        await self.call('Page.enable', session=self.session)

    def _read(self, fd):
        buffer = b''
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buffer += chunk
                while b'\0' in buffer:
                    raw, buffer = buffer.split(b'\0', 1)
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    self.loop.call_soon_threadsafe(self._dispatch, message)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            self.loop.call_soon_threadsafe(self._dispatch, {'closed': True})

    def _dispatch(self, message):
        if message.get('closed'):
            for future in self.waiting.values():
                if not future.done():
                    future.set_exception(HumanError('browser_closed', 'Браузер закрыт', 'Открой страницу заново.'))
            self.proc = None
            return
        future = self.waiting.pop(message.get('id'), None)
        if future and not future.done():
            if 'error' in message:
                future.set_exception(HumanError('browser_cdp', 'Браузер не выполнил команду', str(message['error'].get('message'))[:200]))
            else:
                future.set_result(message.get('result') or {})
        elif 'method' in message:
            self.events.put_nowait(message)

    async def call(self, method, params=None, session=None, timeout=20.0):
        self.ids += 1
        ident = self.ids
        future = self.loop.create_future()
        self.waiting[ident] = future
        payload = {'id': ident, 'method': method, 'params': params or {}}
        if session:
            payload['sessionId'] = session
        try:
            os.write(self.write_fd, json.dumps(payload).encode() + b'\0')
        except OSError:
            raise HumanError('browser_closed', 'Браузер закрыт', 'Открой страницу заново.') from None
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self.waiting.pop(ident, None)
            raise HumanError('browser_timeout', 'Браузер не ответил вовремя', method) from None

    async def goto(self, url):
        while not self.events.empty():
            self.events.get_nowait()
        await self.call('Page.navigate', {'url': url}, self.session)
        try:
            while True:
                event = await asyncio.wait_for(self.events.get(), 20)
                if event.get('method') == 'Page.loadEventFired':
                    break
        except asyncio.TimeoutError:
            pass  # страница грузится долго — читаем, что успело

    async def run(self, expression):
        result = await self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True}, self.session)
        return (result.get('result') or {}).get('value')

    async def snapshot(self):
        return json.loads(await self.run(SNAPSHOT) or '{}')

    def alive(self):
        return bool(self.proc and self.proc.poll() is None)

    async def close(self):
        proc, self.proc = self.proc, None
        for fd in (self.write_fd,):
            try:
                if fd is not None:
                    os.close(fd)
            except OSError:
                pass
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.get_running_loop().run_in_executor(None, proc.wait, 5)
            except Exception:  # noqa: BLE001
                proc.kill()


def render(page, note=''):
    body = f"{page.get('title') or '(без заголовка)'}\n{page.get('url')}\n\n{page.get('text') or ''}\n\nЭлементы:\n" + '\n'.join(page.get('items') or ['(нет)'])
    return untrusted('браузер ' + str(page.get('url'))[:120], (note + '\n' if note else '') + body)


async def browser_of(ctx: ToolContext) -> Chrome:
    cfg = ctx.app.settings.group('browser')
    if not cfg['enabled']:
        raise HumanError('browser_off', 'Браузер агента выключен', 'Включи его в Настройки → Браузер агента (превью).')
    state = getattr(ctx.app, '_browser', None)
    if state is not None and state.alive():
        return state
    exe = find_browser()
    if not exe:
        raise HumanError('browser_missing', 'Не нашёл Chrome, Edge или Chromium', 'Установи один из них или укажи путь в переменной BENDER_BROWSER.')
    chrome = Chrome(exe, ctx.app.dirs.local / 'browser', bool(cfg['headless']))
    await chrome.start()
    ctx.app._browser = chrome
    return chrome


def register(reg) -> None:
    async def guarded(ctx, make):
        try:
            return await make()
        except HumanError as exc:
            if exc.code == 'denied':
                raise
            return ToolResult(exc.title + '. ' + exc.hint, ok=False, summary=exc.title, tainted=False)

    @reg.tool('browser_open', 'Открыть страницу в браузере', group='web')
    async def browser_open(ctx: ToolContext, url: str) -> ToolResult:
        """Открыть адрес в браузере агента и показать страницу текстом с пронумерованными элементами. Для чтения без действий проще web_fetch.

        url — адрес http(s)
        """
        async def make():
            check_url(ctx, url)
            await ctx.check('net.read', url, 'открыть страницу в браузере: ' + url[:100])
            chrome = await browser_of(ctx)
            await chrome.goto(url)
            return ToolResult(render(await chrome.snapshot()), summary='страница открыта', tainted=True)
        return await guarded(ctx, make)

    @reg.tool('browser_look', 'Показать страницу браузера', group='web')
    async def browser_look(ctx: ToolContext) -> ToolResult:
        """Заново показать текущую страницу браузера (после ожидания или действий)."""
        async def make():
            chrome = await browser_of(ctx)
            return ToolResult(render(await chrome.snapshot()), summary='страница', tainted=True)
        return await guarded(ctx, make)

    @reg.tool('browser_click', 'Нажать на странице', group='web')
    async def browser_click(ctx: ToolContext, ref: int) -> ToolResult:
        """Нажать на элемент по номеру из снимка страницы; в ответ новый снимок.

        ref — номер элемента из последнего снимка
        """
        async def make():
            chrome = await browser_of(ctx)
            page = await chrome.snapshot()
            await ctx.check('net.send', str(page.get('url')), f'нажать на элемент {int(ref)} страницы {str(page.get("url"))[:80]}')
            found = await chrome.run(f'(() => {{ const e = document.querySelector(\'[data-bref="{int(ref)}"]\'); if (!e) return false; e.scrollIntoView({{block: "center"}}); e.click(); return true; }})()')
            if not found:
                return ToolResult('Элемента с таким номером нет: страница изменилась. Вызови browser_look.', ok=False, summary='нет элемента')
            await asyncio.sleep(1.2)
            return ToolResult(render(await chrome.snapshot()), summary='нажато', tainted=True)
        return await guarded(ctx, make)

    @reg.tool('browser_type', 'Ввести текст на странице', group='web')
    async def browser_type(ctx: ToolContext, ref: int, text: str, submit: bool = False) -> ToolResult:
        """Ввести текст в поле по номеру из снимка; submit=true — отправить форму (Enter).

        ref — номер поля
        text — что ввести
        submit — отправить форму после ввода
        """
        async def make():
            chrome = await browser_of(ctx)
            page = await chrome.snapshot()
            await ctx.check('net.send', str(page.get('url')), f'ввести текст в поле {int(ref)} страницы {str(page.get("url"))[:80]}', detail={'text': text[:200], 'submit': bool(submit)})
            js = (f'(() => {{ const e = document.querySelector(\'[data-bref="{int(ref)}"]\'); if (!e) return false; e.focus();'
                  f' const s = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(e), "value"); if (s && s.set) s.set.call(e, {json.dumps(text)}); else e.value = {json.dumps(text)};'
                  ' e.dispatchEvent(new Event("input", {bubbles: true})); e.dispatchEvent(new Event("change", {bubbles: true}));'
                  + (' if (e.form) e.form.requestSubmit(); else e.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true}));' if submit else '') + ' return true; })()')
            if not await chrome.run(js):
                return ToolResult('Поля с таким номером нет: страница изменилась. Вызови browser_look.', ok=False, summary='нет поля')
            await asyncio.sleep(1.2 if submit else 0.2)
            return ToolResult(render(await chrome.snapshot()), summary='введено', tainted=True)
        return await guarded(ctx, make)

    @reg.tool('browser_close', 'Закрыть браузер', group='web')
    async def browser_close(ctx: ToolContext) -> ToolResult:
        """Закрыть браузер агента (профиль с входами сохраняется)."""
        state = getattr(ctx.app, '_browser', None)
        if state is not None:
            await state.close()
            ctx.app._browser = None
        return ToolResult('Браузер закрыт.', summary='закрыт')
