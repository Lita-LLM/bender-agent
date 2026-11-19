"""Браузер агента (превью): настоящий Chromium по каналу DevTools, если он есть в системе."""
import threading
import os
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bender.tools.browser import find_browser, launch_args
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv

PAGE = '<html><title>Проба</title><body><h1>Привет</h1><a href="/two">Дальше</a><form action="/two"><input name="q" placeholder="Поиск"><button>Найти</button></form></body></html>'.encode()
LONG = ('<html><title>Длинная</title><body><select name="c"><option value="r">Красный</option><option value="b">Синий</option></select>'
        '<input type="password" name="pw" placeholder="Пароль"><div style="height:3000px">Низ</div><button id="end">Конец</button></body></html>').encode()
FAKE = ('<html><title>Подделка</title><body><input name="login" placeholder="Логин">'
        '<script>const j=JSON.stringify;JSON.stringify=(o,...r)=>{if(o&&o.url!==undefined)o.url="https://bank.example/login";return j(o,...r)}</script></body></html>').encode()
TWO = '<html><title>Вторая</title><body><p>Вы пришли</p></body></html>'.encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = TWO if self.path.startswith('/two') else FAKE if self.path.startswith('/fake') else LONG if self.path.startswith('/long') else PAGE
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class BrowserTest(TempDirCase):
    @async_test
    async def test_off_by_default_and_closed_schemes(self):
        async with MindEnv(self.tmp) as env:
            host = env.app
            ctx = ToolContext(host, session_id=env.sid)
            result = await host.tools.run('browser_open', {'url': 'http://example.com'}, ctx)
            self.assertFalse(result.ok)
            self.assertIn('выключен', result.content)
            host.settings.set('browser', {'enabled': True}, by='owner')
            for url in ('file:///etc/passwd', 'chrome://settings', 'javascript:alert(1)'):
                result = await host.tools.run('browser_open', {'url': url}, ctx)
                self.assertFalse(result.ok, url)
                self.assertIn('http(s)', result.content)
            # Охота 16.11: Python и Chromium по-разному читают «\\» и логин в адресе.
            for url in ('http://evil.example\\@allowed.org/', 'http://user@allowed.org/', 'http://127.0.0.1:8770\\@x.org/'):
                result = await host.tools.run('browser_open', {'url': url}, ctx)
                self.assertFalse(result.ok, url)
                self.assertIn('обратной косой', result.content)

    @unittest.skipUnless(os.name=='posix' and find_browser(), 'Нужен POSIX-канал DevTools и Chrome/Chromium; Windows-канал ещё не реализован')
    @async_test
    async def test_real_page_click_type_and_untrusted_wrapper(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/'
        try:
            async with MindEnv(self.tmp) as env:
                env.auto_approve('once')
                host = env.app
                host.settings.set('browser', {'enabled': True}, by='owner')
                ctx = ToolContext(host, session_id=env.sid)
                opened = await host.tools.run('browser_open', {'url': url}, ctx)
                self.assertTrue(opened.ok, opened.content)
                self.assertTrue(opened.tainted)
                self.assertIn('НЕДОВЕРЕННОЕ', opened.content)
                self.assertIn('Привет', opened.content)
                self.assertIn('ссылка «Дальше»', opened.content)
                self.assertIn('поле «Поиск»', opened.content)
                link = next(line for line in opened.content.splitlines() if 'Дальше' in line and line.startswith('['))
                clicked = await host.tools.run('browser_click', {'ref': int(link[1:link.index(']')])}, ctx)
                self.assertIn('Вы пришли', clicked.content)
                await host.tools.run('browser_open', {'url': url}, ctx)
                typed = await host.tools.run('browser_type', {'ref': 3, 'text': 'кот', 'submit': True}, ctx)
                self.assertIn('Вы пришли', typed.content, typed.content)
                missing = await host.tools.run('browser_click', {'ref': 999}, ctx)
                self.assertFalse(missing.ok)
                await host.tools.run('browser_close', {}, ctx)
        finally:
            server.shutdown()
            server.server_close()


    def test_launch_args_posix_and_windows_handles(self):
        posix = launch_args('/usr/bin/chromium', '/p', True, root=True)
        self.assertIn('--remote-debugging-pipe', posix)
        self.assertIn('--no-sandbox', posix)
        self.assertFalse([a for a in posix if a.startswith('--remote-debugging-io-pipes')])
        win = launch_args('msedge.exe', 'C:/p', False, handles=(412, 416))
        self.assertIn('--remote-debugging-io-pipes=412,416', win)
        self.assertNotIn('--headless=new', win)
        self.assertNotIn('--no-sandbox', win)

    @unittest.skipUnless(os.name=='posix' and find_browser(), 'Нужен POSIX-канал DevTools и Chrome/Chromium')
    @async_test
    async def test_real_press_scroll_back_select_screenshot_and_secret(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}/'
        try:
            async with MindEnv(self.tmp) as env:
                env.auto_approve('once')
                host = env.app
                host.settings.set('browser', {'enabled': True, 'idle_minutes': 5}, by='owner')
                ctx = ToolContext(host, session_id=env.sid)
                run = lambda name, args=None: host.tools.run(name, args or {}, ctx)
                await run('browser_open', {'url': base})
                await run('browser_open', {'url': base + 'long'})
                look = await run('browser_look')
                self.assertIn('список', look.content)
                self.assertNotIn('hunter2', look.content)
                def ref(text):
                    line = next(l for l in look.content.splitlines() if text in l and l.startswith('['))
                    return int(line[1:line.index(']')])
                chosen = await run('browser_select', {'ref': ref('список'), 'option': 'Синий'})
                self.assertTrue(chosen.ok, chosen.content)
                bad = await run('browser_select', {'ref': ref('список'), 'option': 'Зелёный'})
                self.assertFalse(bad.ok)
                scrolled = await run('browser_scroll', {'direction': 'down'})
                self.assertTrue(scrolled.ok)
                shot = await run('browser_screenshot')
                self.assertTrue(shot.ok, shot.content)
                self.assertEqual(shot.model_parts[1]['media_type'], 'image/jpeg')
                self.assertGreater(len(shot.model_parts[1]['data']), 500)
                self.assertFalse((await run('browser_press', {'key': 'F13'})).ok)
                self.assertTrue((await run('browser_press', {'key': 'Tab'})).ok)
                back = await run('browser_back')
                self.assertTrue(back.ok, back.content)
                self.assertIn('Привет', back.content)
                # пароль из Сейфа: только на привязанный хост, в снимке и ответе его нет
                host.secrets.set('site:pw', 'hunter2', allow={'hosts': ['127.0.0.1']})
                await run('browser_open', {'url': base + 'long'})
                look = await run('browser_look')
                typed = await run('browser_type', {'ref': ref('Пароль'), 'text': '{{secret:site:pw}}'})
                self.assertTrue(typed.ok, typed.content)
                self.assertNotIn('hunter2', typed.content)
                self.assertEqual(await host._browser.run('document.querySelector("[name=pw]").value'), 'hunter2')
                host.secrets.set('site:pw', 'hunter2', allow={'hosts': ['other.example']})
                refused = await run('browser_type', {'ref': ref('Пароль'), 'text': '{{secret:site:pw}}'})
                self.assertFalse(refused.ok)
                self.assertNotIn('hunter2', refused.content)
                await run('browser_close')
        finally:
            server.shutdown()
            server.server_close()


class BrowserUrlTest(TempDirCase):
    @unittest.skipUnless(os.name=='posix' and find_browser(), 'Нужен Chrome/Chromium')
    @async_test
    async def test_page_cannot_fake_its_url(self):
        """Охота 16.11: адрес для проверок берётся у браузера, а не из снимка, который считает страница."""
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/fake'
        try:
            async with MindEnv(self.tmp) as env:
                env.auto_approve('once')
                host = env.app
                host.settings.set('browser', {'enabled': True}, by='owner')
                ctx = ToolContext(host, session_id=env.sid)
                opened = await host.tools.run('browser_open', {'url': url}, ctx)
                self.assertTrue(opened.ok, opened.content)
                self.assertNotIn('bank.example', opened.content)
                self.assertIn('127.0.0.1', opened.content)
                await host.tools.run('browser_close', {}, ctx)
        finally:
            server.shutdown()
            server.server_close()
