"""Мягкая переписка: отказ объясняет, что делать, а просьба об открытии связи доходит до хозяина."""
import asyncio

from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class LinkRequestTest(TempDirCase):
    def prepare(self, env, closed=True):
        app = env.app
        app.settings.set('memory', {'review': False, 'compact': False})
        app.agents.create('Второй', agent_id='second', awaken=False, profile='helper')
        if closed:
            app.settings.set('agent_chat', {'default_mode': 'no'}, by='owner')
        return app, ToolContext(app, session_id=env.sid)

    @async_test
    async def test_closed_link_explains_and_agent_can_ask_owner(self):
        async with MindEnv(self.tmp) as env:
            app, ctx = self.prepare(env)
            denied = await app.tools.run('message_agent', {'agent': 'second', 'text': 'Привет'}, ctx)
            self.assertFalse(denied.ok)
            self.assertIn('request_link', denied.content, 'отказ подсказывает мягкий путь')
            self.assertIn('Второй', denied.content)
            rows = (await app.tools.run('agents', {}, ctx)).data['agents']
            self.assertIn('request_link', next(r for r in rows if r['id'] == 'second')['hint'])
            asked = await app.tools.run('request_link', {'agent': 'second', 'why': 'Хочу сверить с ним план недели'}, ctx)
            self.assertTrue(asked.ok, asked.content)
            self.assertEqual(asked.data['state'], 'asked')
            await asyncio.sleep(0.3)  # уведомление уходит фоновой задачей
            texts = [m['content'] for m in app.store.messages(app.main_session())]
            self.assertTrue(any('просит открыть связь' in t and 'план недели' in t for t in texts), texts)
            again = await app.tools.run('request_link', {'agent': 'second', 'why': 'Ещё раз'}, ctx)
            self.assertEqual(again.data['state'], 'waiting', 'частые просьбы не засыпают хозяина')

    @async_test
    async def test_request_for_open_link_and_empty_reason(self):
        async with MindEnv(self.tmp) as env:
            app, ctx = self.prepare(env)
            empty = await app.tools.run('request_link', {'agent': 'second', 'why': '  '}, ctx)
            self.assertFalse(empty.ok)
            app.settings.set('agent_chat', {'matrix': [{'sender': 'main', 'recipient': 'second', 'mode': 'yes', 'per_hour': 30, 'reply': True}]})
            already = await app.tools.run('request_link', {'agent': 'second', 'why': 'Нужна связь'}, ctx)
            self.assertEqual(already.data['state'], 'open')

    @async_test
    async def test_new_pairs_are_open_by_default(self):
        async with MindEnv(self.tmp) as env:
            app, ctx = self.prepare(env, closed=False)
            self.assertEqual(app.agents.correspondence.edge('main', 'second')['mode'], 'yes')
            rows = (await app.tools.run('agents', {}, ctx)).data['agents']
            self.assertIn('можно писать', next(r for r in rows if r['id'] == 'second')['hint'])
            app.settings.set('agent_chat', {'matrix': [{'sender': 'main', 'recipient': 'second', 'mode': 'no', 'per_hour': 30, 'reply': True}]}, by='owner')
            self.assertEqual(app.agents.correspondence.edge('main', 'second')['mode'], 'no', 'явная строка сильнее умолчания')

    @async_test
    async def test_owner_button_opens_link_and_tells_the_agent(self):
        async with MindEnv(self.tmp) as env:
            app, ctx = self.prepare(env)
            await app.tools.run('request_link', {'agent': 'second', 'why': 'Сверить планы'}, ctx)
            await asyncio.sleep(0.3)
            asked = [m for m in app.store.messages(app.main_session()) if (m.get('meta') or {}).get('link_request')]
            self.assertEqual(asked[0]['meta']['link_request']['sender'], 'main')
            app.agents.correspondence.set_link('main', 'second', 'yes')
            self.assertEqual(app.agents.correspondence.edge('main', 'second')['mode'], 'yes')
            told = [m['content'] for m in app.store.messages(app.main_session())]
            self.assertTrue(any('открыл тебе связь' in t for t in told), told)
            app.agents.correspondence.set_link('main', 'second', 'no')
            self.assertEqual(app.agents.correspondence.edge('main', 'second')['mode'], 'no')
            self.assertEqual(len(app.settings.group('agent_chat')['matrix']), 1, 'строка пары обновляется, а не дублируется')
