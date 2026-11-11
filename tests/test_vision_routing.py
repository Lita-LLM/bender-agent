"""Картинка при неизвестном зрении главной: роутер (как OmniRoute) ведёт к разным моделям — описание Зрением рядом."""
import asyncio
import json

from bender.core.settings import NeedsOwner
from bender.router.capabilities import description
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv

IMAGE = {'type': 'image', 'data': 'eA==', 'media_type': 'image/png', 'path': 'inbox/снимок.png'}


class VisionRoutingTest(TempDirCase):
    def setup_roles(self, env, main, vision=('seer',)):
        app = env.app
        pid = app.models.roles()['main'][0]['provider']
        app.models.set_role('main', [{'provider': pid, 'model': main}])
        app.models.set_role('vision', [{'provider': pid, 'model': m} for m in vision])
        return pid

    def calls(self, env):
        return [r['body'] for r in env.fake.state.requests if r.get('api') == 'openai']

    async def ask(self, env, text='Что на картинке?'):
        content = [{'type': 'text', 'text': text}, dict(IMAGE)]
        return await asyncio.wait_for(env.app.mind.say(env.sid, text, content=content), 10)

    @staticmethod
    def last(body):
        """Последнее сообщение хозяина в запросе (история и паспорт не в счёт)."""
        return next(m for m in reversed(body['messages']) if m.get('role') == 'user')

    def images(self, body):
        content = self.last(body).get('content')
        return sum(1 for p in content if isinstance(p, dict) and p.get('type') == 'image_url') if isinstance(content, list) else 0

    def said(self, body):
        return json.dumps(self.last(body), ensure_ascii=False)

    @async_test
    async def test_router_that_silently_drops_images_gets_vision_description_by_default(self):
        async with MindEnv(self.tmp) as env:
            self.setup_roles(env, 'blind')
            result = await self.ask(env)
            self.assertIsNone(result['error'], result)
            bodies = self.calls(env)
            self.assertEqual([b['model'] for b in bodies], ['seer', 'blind'])
            main = bodies[-1]
            self.assertEqual(self.images(main), 1)  # картинка осталась: вдруг за роутером модель со зрением
            text = self.said(main)
            self.assertIn('Вижу изображений: 1', text)
            self.assertIn('недоверенные данные', text)
            self.assertIn('НЕДОВЕРЕННОЕ: описание изображения', text)
            self.assertIn('inbox/снимок.png', text)
            self.assertTrue(env.app.store.session(env.sid)['meta']['last_turn']['tainted'])
            self.assertEqual(env.app.store.one("SELECT count(*) FROM calls WHERE role='vision'")[0], 1)

    @async_test
    async def test_describe_mode_sends_only_description_and_image_mode_only_picture(self):
        async with MindEnv(self.tmp) as env:
            self.setup_roles(env, 'blind')
            env.app.settings.set('attachments', {'vision_unknown': 'describe'})
            await self.ask(env)
            main = self.calls(env)[-1]
            self.assertEqual(main['model'], 'blind')
            self.assertEqual(self.images(main), 0)
            self.assertIn('Вижу изображений: 1', self.said(main))
            env.fake.state.requests.clear()
            env.app.settings.set('attachments', {'vision_unknown': 'image'})
            await self.ask(env, 'А теперь?')
            self.assertEqual([b['model'] for b in self.calls(env)], ['blind'])
            self.assertEqual(self.images(self.calls(env)[-1]), 1)

    @async_test
    async def test_no_description_without_other_vision_model(self):
        async with MindEnv(self.tmp) as env:
            self.setup_roles(env, 'blind', vision=())
            await self.ask(env)
            self.assertEqual([b['model'] for b in self.calls(env)], ['blind'])
            self.assertNotIn('Описание изображения', self.said(self.calls(env)[-1]))
            env.fake.state.requests.clear()
            self.setup_roles(env, 'blind', vision=('blind',))  # Зрение — та же модель: описывать себе нечего
            await self.ask(env, 'Ещё?')
            self.assertEqual([b['model'] for b in self.calls(env)], ['blind'])

    @async_test
    async def test_model_that_sees_and_model_that_refuses(self):
        async with MindEnv(self.tmp) as env:
            pid = self.setup_roles(env, 'seer')
            env.app.models.set_capability(pid, 'seer', 'vision', True, source='owner')
            await self.ask(env)
            self.assertEqual([b['model'] for b in self.calls(env)], ['seer'])  # видит сама — Зрение не зовём
            self.assertIn('Вижу изображений: 1', env.app.store.messages(env.sid)[-1]['content'])
            env.fake.state.requests.clear()
            self.setup_roles(env, 'noimage')
            result = await self.ask(env, 'А эта?')
            self.assertIsNone(result['error'], result)
            models = [b['model'] for b in self.calls(env)]
            self.assertEqual(models[0], 'seer')  # описание до главной
            self.assertEqual(models.count('noimage'), 2)  # отказ на картинку → повтор без неё
            self.assertEqual(self.images(self.calls(env)[-1]), 0)
            self.assertIn('Вижу изображений: 1', self.said(self.calls(env)[-1]))
            self.assertIs(description(env.app.models.providers()[pid], 'noimage', 'vision')['value'], False)

    @async_test
    async def test_agent_cannot_turn_on_paid_descriptions_alone(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('attachments', {'vision_unknown': 'image'})
            with self.assertRaises(NeedsOwner):
                env.app.settings.set('attachments', {'vision_unknown': 'both'}, by='agent')
            env.app.settings.set('attachments', {'vision_unknown': 'both'})
            env.app.settings.set('attachments', {'vision_unknown': 'image'}, by='agent')  # дешевле — можно

    @async_test
    async def test_doctor_says_who_describes_images_without_model_calls(self):
        from bender.diagnostics.doctor import collect
        async with MindEnv(self.tmp) as env:
            app = env.app
            pid = self.setup_roles(env, 'blind', vision=())
            row = lambda: next(r for r in collect(app.dirs.root)['rows'] if r['id'] == 'vision')
            self.assertEqual(row()['level'], 'warn'); self.assertIn('роутер', row()['action'])
            self.setup_roles(env, 'blind')
            self.assertEqual((row()['level'], row()['title']), ('ok', 'Картинки: неизвестно, видит ли главная'))
            app.models.set_capability(pid, 'blind', 'vision', False, source='rejected', reason='секретная причина https://example.invalid')
            self.setup_roles(env, 'blind', vision=())
            self.assertEqual((row()['level'], row()['title']), ('warn', 'Картинки: главная не видит'))
            self.assertIn('Запомнен отказ', row()['action']); self.assertNotIn('секретная', row()['action'])
            self.assertEqual([r for r in env.fake.state.requests if r.get('api') == 'openai'], [])
