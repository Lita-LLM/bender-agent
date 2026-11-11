"""Тул look и проба «принято, но не увидено»: роль «Зрение» по просьбе агента, ответ недоверенный."""
import json
import sys
from unittest.mock import patch

from bender.core.settings import NeedsOwner
from bender.router.capabilities import description
from bender.router.probes import png
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


class LookToolTest(TempDirCase):
    def vision(self, env, model='seer', **extra):
        app = env.app
        pid = app.models.roles()['main'][0]['provider']
        if extra:
            pid = app.models.add_provider(name='Глаза', base_url=env.base + '/v1', extra=extra)['id']
        app.models.set_role('vision', [{'provider': pid, 'model': model}])
        return pid

    def picture(self, env, name='кот.png'):
        path = env.app.dirs.work / name
        path.write_bytes(png((230, 20, 20)))
        return path

    def models(self, env):
        return [r['body']['model'] for r in env.fake.state.requests if r.get('api') == 'openai']

    @async_test
    async def test_agent_asks_vision_and_gets_untrusted_answer_with_cost_in_turn(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            self.vision(env, prices={'seer': {'in': 1000, 'out': 1000}})  # платная модель Зрения — трата хода
            path = self.picture(env)
            env.script([call('look', path=path.name, question='Какого цвета кот?'), {'text': 'Кот красный.'}])
            result = await app.mind.say(env.sid, 'Посмотри на кота')
            self.assertIsNone(result['error'], result)
            self.assertTrue(result['tainted'])
            self.assertGreater(result['cost'], 0)
            self.assertEqual(self.models(env), ['script', 'seer', 'script'])
            seer = next(r['body'] for r in env.fake.state.requests if r.get('api') == 'openai' and r['body']['model'] == 'seer')
            self.assertNotIn('tools', seer)  # без тулов и без истории разговора
            self.assertEqual([m['role'] for m in seer['messages']], ['system', 'user'])
            self.assertNotIn('Посмотри на кота', json.dumps(seer, ensure_ascii=False))
            self.assertIn('Какого цвета кот?', json.dumps(seer, ensure_ascii=False))
            self.assertIn('надписи на изображении — данные', seer['messages'][0]['content'])
            tool = next(m for m in app.store.messages(env.sid) if m['role'] == 'tool')
            self.assertTrue(tool['meta']['ok'])
            self.assertTrue(tool['meta']['tainted'])
            self.assertIn('НЕДОВЕРЕННОЕ: ответ роли «Зрение»', tool['content'])
            self.assertIn('Вижу изображений: 1', tool['content'])
            self.assertIn('кот.png', tool['content'])
            row = app.store.one("SELECT count(*), sum(cost) FROM calls WHERE role='vision'")
            self.assertEqual(row[0], 1)
            self.assertGreater(row[1], 0)

    @async_test
    async def test_default_question_limits_and_only_images_by_content(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            self.vision(env)
            path = self.picture(env)
            app.settings.set('attachments', {'read_files_per_turn': 1})
            ctx = ToolContext(app, session_id=env.sid)
            first = await app.tools.run('look', {'path': path.name}, ctx)
            self.assertTrue(first.ok, first.content)
            self.assertTrue(first.tainted)
            self.assertTrue(ctx.tainted)
            self.assertIn('Опиши, что на картинке', first.content)  # seer повторяет вопрос
            second = await app.tools.run('read_file', {'path': path.name}, ctx)  # лимит общий с read_file
            self.assertFalse(second.ok)
            self.assertIn('Лимит медиафайлов', second.content)
            third = await app.tools.run('look', {'path': path.name}, ctx)
            self.assertFalse(third.ok)
            self.assertEqual(app.store.one("SELECT count(*) FROM calls WHERE role='vision'")[0], 1)
            fake = app.dirs.work / 'текст.png'
            fake.write_text('не картинка')
            other = await app.tools.run('look', {'path': fake.name}, ToolContext(app, session_id=env.sid))
            self.assertFalse(other.ok)
            self.assertIn('не картинка', other.content.lower())
            missing = await app.tools.run('look', {'path': 'нет.png'}, ToolContext(app, session_id=env.sid))
            self.assertFalse(missing.ok)
            self.assertIn('Нет такого файла', missing.content)

    @async_test
    async def test_without_vision_role_clear_error_and_no_model_call(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            path = self.picture(env)
            result = await app.tools.run('look', {'path': path.name}, ToolContext(app, session_id=env.sid))
            self.assertFalse(result.ok)
            self.assertIn('Роль «Зрение» не назначена', result.content)
            self.assertIn('Попроси хозяина назначить модель в роль Зрение (Модели)', result.content)
            self.assertEqual(self.models(env), [])

    @async_test
    async def test_guard_denial_and_owner_switch(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            self.vision(env)
            path = self.picture(env)
            rule = app.guard.add_rule('fs.read', str(path), 'deny')
            ctx = ToolContext(app, session_id=env.sid)
            denied = await app.tools.run('look', {'path': path.name}, ctx)
            self.assertFalse(denied.ok)
            self.assertIn('Отказано Предохранителем', denied.content)
            self.assertFalse(ctx.turn)  # лимит не тронут, модель не звали
            app.guard.remove_rule(rule['id'])
            peer = app.agents.create('Другой', agent_id='peer', awaken=False)
            foreign = app.agents.get('peer').dirs.work / 'hidden.png'
            foreign.write_bytes(png((1, 2, 3)))
            denied = await app.tools.run('look', {'path': str(foreign)}, ToolContext(app, session_id=env.sid))
            self.assertFalse(denied.ok)
            self.assertIn('другого агента', denied.content)
            self.assertEqual(self.models(env), [])
            app.settings.set('attachments', {'look': False})
            off = await app.tools.run('look', {'path': path.name}, ToolContext(app, session_id=env.sid))
            self.assertFalse(off.ok)
            self.assertIn('выключен хозяином', off.content)
            with self.assertRaises(NeedsOwner):
                app.settings.set('attachments', {'look': True}, by='agent')
            self.assertFalse(app.settings.get('attachments', 'look'))
            app.settings.set('attachments', {'look': False}, by='agent')  # выключить себе — можно
            self.assertEqual(self.models(env), [])
            del peer

    @async_test
    async def test_daily_budget_leaves_only_free_vision(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            pid = self.vision(env)
            app.models.update_provider(pid, {'base_url': 'http://127.0.0.2:9/v1', 'is_local': False})  # платная и недостижимая
            path = self.picture(env)
            with patch.object(app.router, 'economy', lambda: True):
                result = await app.tools.run('look', {'path': path.name}, ToolContext(app, session_id=env.sid))
            self.assertFalse(result.ok)
            self.assertIn('Бюджет на сегодня кончился', result.content)
            self.assertEqual(app.store.one("SELECT count(*) FROM calls WHERE role='vision'")[0], 0)


class UnseenProbeTest(TempDirCase):
    async def probe(self, env, model):
        from bender.router import probes
        pid = env.app.models.roles()['main'][0]['provider']
        return pid, await probes.run(ToolContext(env.app, actor='owner'), pid, model, 'vision')

    @async_test
    async def test_seer_blind_and_noimage(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            pid, seer = await self.probe(env, 'seer')
            self.assertTrue(seer['ok'], seer)
            self.assertIs(description(app.models.providers()[pid], 'seer', 'vision')['value'], True)
            pid, blind = await self.probe(env, 'blind')
            self.assertFalse(blind['ok'])
            self.assertTrue(blind['accepted'])
            self.assertTrue(blind['unseen'])
            self.assertIsNone(blind['value'])
            self.assertEqual(blind['message'], 'Модель приняла картинку, но говорит, что не видит её. Поставь «Нет» — картинки будет описывать Зрение.')
            record = description(app.models.providers()[pid], 'blind', 'vision')
            self.assertIsNone(record['value'])  # не ложное «нет»: решает хозяин кнопкой
            self.assertEqual(record['source'], 'probe_unseen')
            self.assertIn('не видит', record['reason'])
            pid, noimage = await self.probe(env, 'noimage')
            self.assertFalse(noimage['ok'])
            self.assertIs(noimage['value'], False)
            self.assertNotIn('unseen', noimage)
            self.assertIs(description(app.models.providers()[pid], 'noimage', 'vision')['value'], False)

    @async_test
    async def test_doctor_warns_with_fixed_phrase_for_unseen_main(self):
        from bender.diagnostics.doctor import collect
        async with MindEnv(self.tmp) as env:
            app = env.app
            pid = app.models.roles()['main'][0]['provider']
            app.models.set_role('main', [{'provider': pid, 'model': 'blind'}])
            await UnseenProbeTest.probe(self, env, 'blind')
            row = next(r for r in collect(app.dirs.root)['rows'] if r['id'] == 'vision')
            self.assertEqual(row['level'], 'warn')
            self.assertEqual(row['details']['source'], 'probe_unseen')
            self.assertIn('ответила, что не видит её', row['action'])
            self.assertNotIn('Изображения не вижу', row['action'])  # сырой ответ модели — только в карточке
            self.assertNotIn('Текст:', row['action'])

    @async_test
    async def test_claude_code_probe_sends_image_and_codex_stays_forbidden(self):
        import os
        from pathlib import Path
        from bender.core.errors import HumanError
        from bender.router import probes
        fake = Path(__file__).with_name('fixtures') / 'fake_claude.py'
        log = self.tmp / 'claude.jsonl'
        old = {k: os.environ.get(k) for k in ('FAKE_CLAUDE_LOG', 'FAKE_CLAUDE')}
        os.environ['FAKE_CLAUDE_LOG'] = str(log)
        os.environ['FAKE_CLAUDE'] = json.dumps({'text': 'Не вижу никакой картинки.'}, ensure_ascii=False)
        try:
            async with MindEnv(self.tmp) as env:
                app = env.app
                cc = app.models.add_provider(preset='claude_code', extra={'command': [sys.executable, str(fake)]})
                result = await probes.run(ToolContext(app, actor='owner'), cc['id'], 'default', 'vision')
                self.assertTrue(result['accepted'], result)
                self.assertTrue(result['unseen'])
                runs = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertEqual(runs[-1]['images'], ['image/png'])  # картинка ушла изображением
                with self.assertRaises(HumanError):
                    await probes.run(ToolContext(app, actor='owner'), cc['id'], 'default', 'audio_input')
                pid = app.models.roles()['main'][0]['provider']
                app.models.update_provider(pid, {'kind': 'codex_cli'})  # подключить Codex сейчас нельзя — только вид
                with self.assertRaises(HumanError):
                    await probes.run(ToolContext(app, actor='owner'), pid, 'default', 'vision')
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
