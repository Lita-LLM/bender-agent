"""Короткие описания агентов: хозяин пишет в Стае, агент — о себе; соседи видят их тулом agents."""
import asyncio
import json
import urllib.error
import urllib.request

from bender.core.errors import HumanError
from bender.mind import context as ctxmod
from bender.net.http_server import HttpServer
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test, owner_headers, owner_web
from tests.test_mind import MindEnv


def _request(port, method, path, body=None):
    headers = owner_headers(**({'X-Bender': '1', 'Content-Type': 'application/json'} if body is not None else {}))
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


class AgentCardsTest(TempDirCase):
    def prepare(self, env):
        app = env.app
        app.settings.set('memory', {'review': False, 'compact': False})
        app.agents.create('Второй', agent_id='second', awaken=False, profile='helper')
        return app, app.agents.get('second')

    async def neighbours(self, app, env):
        return json.loads((await app.tools.run('agents', {}, ToolContext(app, session_id=env.sid))).content)

    @async_test
    async def test_owner_writes_and_neighbours_read_it(self):
        async with MindEnv(self.tmp) as env:
            app, second = self.prepare(env)
            app.agents.describe('second', '  Считаю   бюджет\nсемьи  ', by='owner')
            rows = await self.neighbours(app, env)
            row = next(r for r in rows if r['id'] == 'second')
            self.assertEqual(row['description'], 'Считаю бюджет семьи')
            self.assertEqual(row['described_by'], 'owner')
            # и агент стаи видит описания соседей тем же тулом
            mine = await second.tools.run('agents', {}, ToolContext(second, session_id=second.main_session()))
            self.assertIn('Считаю бюджет семьи', mine.content)

    @async_test
    async def test_agent_describes_itself_but_not_over_owner_text(self):
        async with MindEnv(self.tmp) as env:
            app, second = self.prepare(env)
            ctx = ToolContext(second, session_id=second.main_session())
            result = await second.tools.run('describe_me', {'text': 'Веду расписание и напоминания'}, ctx)
            self.assertTrue(result.ok, result.content)
            self.assertEqual(app.agents.record('second')['data'].get('described_by'), 'agent')
            app.agents.describe('second', 'Пишу тексты', by='owner')
            denied = await second.tools.run('describe_me', {'text': 'Что-то другое'}, ctx)
            self.assertFalse(denied.ok)
            self.assertEqual(app.agents.record('second')['data'].get('description'), 'Пишу тексты', 'текст хозяина цел')
            cleared = await second.tools.run('describe_me', {}, ctx)
            self.assertFalse(cleared.ok, 'очистить описание хозяина агент тоже не может')
            app.agents.describe('second', '', by='owner')
            self.assertNotIn('description', app.agents.record('second')['data'], 'пустое описание хозяина убирает запись')
            again = await second.tools.run('describe_me', {'text': 'Слежу за почтой'}, ctx)
            self.assertTrue(again.ok, 'после очистки хозяином агент снова пишет себя')

    @async_test
    async def test_limit_and_switch_hide_descriptions_from_neighbours(self):
        async with MindEnv(self.tmp) as env:
            app, second = self.prepare(env)
            with self.assertRaises(HumanError):
                app.agents.describe('second', 'я ' * 150, by='owner')  # больше 200 знаков
            ctx = ToolContext(second, session_id=second.main_session())
            self.assertFalse((await second.tools.run('describe_me', {'text': 'x' * 201}, ctx)).ok)
            app.agents.describe('second', 'Вожу машину по выходным', by='owner')
            app.settings.set('flock', {'descriptions': False}, by='owner')
            row = next(r for r in await self.neighbours(app, env) if r['id'] == 'second')
            self.assertNotIn('description', row, 'соседи описаний не видят')
            self.assertEqual(row['name'], 'Второй')
            self.assertFalse((await second.tools.run('describe_me', {'text': 'Новое'}, ctx)).ok, 'тул выключен вместе с показом')
            # хозяин своё описание видит в пульте всегда
            self.assertEqual(next(r for r in app.agents.public() if r['id'] == 'second')['description'], 'Вожу машину по выходным')

    @async_test
    async def test_pult_api_sets_description_and_listing_shows_it(self):
        async with MindEnv(self.tmp) as env:
            app, _ = self.prepare(env)
            web = HttpServer(owner_web(app, allowed_hosts={'127.0.0.1'}))
            await web.start('127.0.0.1', 0)
            try:
                port = web.port
                code, row = await asyncio.to_thread(_request, port, 'PATCH', '/api/agents/second', {'description': 'Слежу за почтой'})
                self.assertEqual(code, 200, row)
                _, listing = await asyncio.to_thread(_request, port, 'GET', '/api/agents')
                found = next(a for a in listing['agents'] if a['id'] == 'second')
                self.assertEqual(found['description'], 'Слежу за почтой')
                self.assertEqual(found['described_by'], 'owner')
                code, _ = await asyncio.to_thread(_request, port, 'PATCH', '/api/agents/second', {'description': 'я ' * 150})
                self.assertNotEqual(code, 200, 'слишком длинное описание не сохраняется')
            finally:
                await web.stop()


class OwnerCardTest(TempDirCase):
    def prepare(self, env):
        app = env.app
        app.settings.set('memory', {'review': False, 'compact': False})
        app.agents.create('Второй', agent_id='second', awaken=False, profile='helper')
        return app, app.agents.get('second')

    @async_test
    async def test_every_agent_reads_it_at_the_start(self):
        async with MindEnv(self.tmp) as env:
            app, second = self.prepare(env)
            for ctx in (app, second):
                self.assertNotIn('О хозяине', ctxmod.system_prompt(ctx), 'пока карточка пуста, раздела нет')
            app.settings.set('owner', {'about': 'Меня зовут Миша.\r\nОбращайся на «ты».  '}, by='owner')
            for ctx in (app, second):
                parts = ctxmod.system_prompt(ctx).split('\n\n---\n\n')
                self.assertTrue(parts[0].startswith('# Как ты устроен'))
                self.assertTrue(parts[1].startswith('# О хозяине'), parts[1][:60])
                self.assertIn('Меня зовут Миша.\nОбращайся на «ты».', parts[1])
                self.assertTrue(parts[1].endswith('на «ты».'), 'пробелы по краям убраны')

    @async_test
    async def test_card_is_shared_and_only_owner_writes_it(self):
        async with MindEnv(self.tmp) as env:
            app, second = self.prepare(env)
            # хозяин правит при открытом втором агенте — карточка общая, а не копия второго
            second.settings.set('owner', {'about': 'Правлю, пока открыт второй'}, by='owner')
            self.assertEqual(app.settings.get('owner', 'about'), 'Правлю, пока открыт второй')
            for ctx in (app, second):
                with self.assertRaises(HumanError) as raised:
                    ctx.settings.set('owner', {'about': 'Взлом'}, by='agent')
                self.assertEqual(raised.exception.code, 'owner_only')
            call_main = await app.tools.run('settings', {'group': 'owner', 'changes': {'about': 'Взлом'}}, ToolContext(app, session_id=env.sid))
            call_second = await second.tools.run('settings', {'group': 'owner', 'changes': {'about': 'Взлом'}}, ToolContext(second, session_id=second.main_session()))
            for result in (call_main, call_second):
                self.assertFalse(result.ok, result.content)
                self.assertIn('хозяин', result.content)
            self.assertEqual(app.settings.get('owner', 'about'), 'Правлю, пока открыт второй', 'агенты карточку не изменили')

    @async_test
    async def test_limit_and_pult_marks_it_owner_only(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            with self.assertRaises(HumanError):
                app.settings.set('owner', {'about': 'я' * 2001}, by='owner')
            app.settings.set('owner', {'about': 'я' * 2000}, by='owner')
            group = next(g for g in app.settings.describe() if g['name'] == 'owner')
            field = group['fields'][0]
            self.assertEqual((field['key'], field['type'], field['permission']), ('about', 'text', 'owner'))

    @async_test
    async def test_pult_saves_card_through_settings_api(self):
        async with MindEnv(self.tmp) as env:
            app, _ = self.prepare(env)
            web = HttpServer(owner_web(app, allowed_hosts={'127.0.0.1'}))
            await web.start('127.0.0.1', 0)
            try:
                code, body = await asyncio.to_thread(_request, web.port, 'POST', '/api/settings/owner', {'about': 'Через пульт'})
                self.assertEqual(code, 200, body)
                self.assertIn('Через пульт', ctxmod.system_prompt(app))
                code, _ = await asyncio.to_thread(_request, web.port, 'POST', '/api/settings/owner', {'about': 'я' * 2001})
                self.assertNotEqual(code, 200, 'слишком длинная карточка не сохраняется')
            finally:
                await web.stop()
