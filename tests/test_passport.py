"""Паспорт видит живую конфигурацию, но не секреты и не чужие настройки."""
import json
from unittest.mock import patch

from bender.mind.context import estimate_tokens
from bender.mind.loop import Turn
from bender.mind.passport import brief, snapshot
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


class PassportTest(TempDirCase):
    @async_test
    async def test_model_reads_all_roles_without_secrets(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            secret = 'sk-PASSPORT-НЕ-ПЕЧАТАТЬ-123456'
            app.secrets.set('test', secret)
            pid = app.models.roles()['main'][0]['provider']
            for role in ('fast', 'smart', 'coder'):
                app.models.set_role(role, [{'provider': pid, 'model': 'модель-' + role}])
            app.models.update_provider(pid, {'headers': {'Authorization': secret}, 'base_url': env.base + '/v1'})
            env.script([call('about_me'), {'text': 'Знаю себя.'}])
            result = await app.mind.say(env.sid, 'Кто ты?', channel='telegram')
            self.assertIsNone(result['error'])
            row = next(m for m in app.store.messages(env.sid) if m['role'] == 'tool')
            data = json.loads(row['content'])
            for role in ('fast', 'smart', 'coder'):
                self.assertEqual(data['roles'][role][0]['id'], 'модель-' + role)
            self.assertEqual(data['home_channel'], 'telegram')
            text = json.dumps(data, ensure_ascii=False)
            self.assertNotIn(secret, text)
            self.assertNotIn(str(app.dirs.secrets), text)
            self.assertNotIn('headers', text)
            prompt = env.fake.state.requests[0]['body']['messages'][0]['content']
            self.assertIn('Модель хода: Фейк/script', prompt)
            self.assertNotIn(secret, prompt)
            turn = Turn(env.sid, 'telegram')
            candidate = app.router.chain('main')[0]
            first = brief(app, turn, candidate)
            self.assertIs(first, brief(app, turn, candidate))
            self.assertLess(estimate_tokens(first), 550)
            self.assertIn('без таблиц и заголовков', first)
            self.assertIn('полный markdown', brief(app, Turn(env.sid, 'pult'), candidate))
            self.assertIn('простой текст', brief(app, Turn(env.sid, 'terminal'), candidate))
            app.models.set_role('fast', [{'provider': pid, 'model': 'новая-fast'}])
            self.assertIn('новая-fast', brief(app, turn, candidate))
            a = brief(app, Turn(env.sid, 'subagent', system='роль', tool_names=('read_file',)), candidate)
            b = brief(app, Turn(env.sid, 'subagent', system='роль', tool_names=('run',)), candidate)
            self.assertIn('тулы=read_file', a)
            self.assertIn('тулы=run', b)
            self.assertNotEqual(a, b)

    @async_test
    async def test_actual_sandbox_and_whitelisted_labels(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.secrets.set('test', 'секрет-имя-1234')
            pid = app.models.roles()['main'][0]['provider']
            app.models.update_provider(pid, {'name': 'секрет-имя-1234 https://user:pass@example.com'})
            with patch('bender.guard.sandbox.detect', return_value={'kind': 'none', 'fs': False, 'net': False, 'title': 'ОС отказала'}):
                data = snapshot(app, detailed=True)
            self.assertEqual(data['sandbox']['kind'], 'none')
            text = json.dumps(data, ensure_ascii=False)
            self.assertNotIn('секрет-имя-1234', text)
            self.assertNotIn('user:pass', text)
            self.assertTrue(data['tools']['self']['count'])
