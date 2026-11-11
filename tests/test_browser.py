"""Браузер агента (превью): настоящий Chromium по каналу DevTools, если он есть в системе."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bender.tools.browser import find_browser
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv

PAGE = '<html><title>Проба</title><body><h1>Привет</h1><a href="/two">Дальше</a><form action="/two"><input name="q" placeholder="Поиск"><button>Найти</button></form></body></html>'.encode()
TWO = '<html><title>Вторая</title><body><p>Вы пришли</p></body></html>'.encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = TWO if self.path.startswith('/two') else PAGE
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

    @unittest.skipUnless(find_browser(), 'нет Chrome/Chromium')
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
