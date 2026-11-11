"""Агент сам сжимает разговор, черновик Telegram закрывается в конце хода, сообщения подписаны знаком агента."""
import asyncio
import json

from bender.agents.badge import line, PALETTE
from bender.mind import compact
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call
import tests.test_telegram as telegram_tests


class SelfServiceTest(TempDirCase):
    _up = telegram_tests.TelegramTest._up
    until = telegram_tests.TelegramTest.until
    sent = telegram_tests.TelegramTest.sent

    @async_test
    async def test_agent_compacts_its_own_conversation_mid_turn(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('memory', {'review': False})
            for i in range(12):
                env.app.store.add_message(env.sid, {'role': 'user', 'content': f'вопрос {i}: ' + 'про проект ' * 20})
                env.app.store.add_message(env.sid, {'role': 'assistant', 'content': f'ответ {i}: ' + 'подробности ' * 20})
            env.script([call('compact'),
                        {'text': json.dumps({'summary': 'Хозяин долго расспрашивал про проект. ПЕРЕСКАЗ-СВОЙ', 'diary': ''}, ensure_ascii=False)},
                        {'text': 'Сжал разговор.'}])
            result = await env.app.mind.say(env.sid, 'сожми разговор')
            self.assertIsNone(result['error']); self.assertIn('Сжал', result['text'])
            self.assertIn('ПЕРЕСКАЗ-СВОЙ', compact.state(env.app, env.sid)['summary'])
            last = [r for r in env.fake.state.requests if r['body'].get('tools')][-1]['body']['messages']
            self.assertIn('ПЕРЕСКАЗ-СВОЙ', json.dumps(last, ensure_ascii=False), 'следующий шаг того же хода уже видит пересказ')
            self.assertNotIn('вопрос 0:', json.dumps(last, ensure_ascii=False))

    @async_test
    async def test_draft_is_closed_when_turn_ends_without_final_message(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('telegram',{'live_via':'draft'})  # проверяется именно черновик Telegram
            app = env.app; await self._up(env, owner=True)
            env.script([{'text': 'Пишу…', **call('send_message', text='Всё готово.', complete=True)},
                        {'text': 'Всё готово.', 'delay': 1.3}])
            await app.mind.say(env.sid, 'сделай', channel='telegram')
            await self.until(lambda: self.sent(env, 'deleteMessage'))
            await self.until(lambda: not app.telegram._bg)
            texts = [m['text'] for m in self.sent(env)]
            self.assertEqual(texts[0], 'Всё готово.'); self.assertEqual(texts[-1], '✓')
            self.assertEqual(len(texts), 2, 'итог не дублируется, закрывающее сообщение одно')
            closing = self.sent(env)[-1]; self.assertTrue(closing.get('disable_notification'))
            self.assertEqual(len(self.sent(env, 'deleteMessage')), 1, '«✓» сразу удаляется — в чате не остаётся')

    @async_test
    async def test_badges_are_unique_stable_and_sign_every_agent_message(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('telegram',{'live_via':'draft'})  # проверяется именно черновик Telegram
            app = env.app; await self._up(env, owner=True)
            app.settings.set('telegram', {'agent_label': True})
            app.selfdir.set_identity({'name': 'Бендер', 'emoji': '🦊'})
            luna = app.agents.get(app.agents.create('Луна', agent_id='luna', awaken=False, start=False)['id'])
            luna.selfdir.set_identity({'emoji': '🦊'})
            self.assertEqual(line(app), '🦊 Бендер')
            first = line(luna); self.assertTrue(first.endswith(' Луна')); self.assertFalse(first.startswith('🦊'))
            self.assertEqual(line(luna), first, 'знак не прыгает')
            self.assertIn(first.split()[0], PALETTE)
            luna.selfdir.set_identity({'emoji': '🐙'}); self.assertEqual(line(luna), '🐙 Луна', 'свободный эмодзи из IDENTITY.md берётся')
            env.script([{'text': 'Ответ хозяину.'}])
            await app.mind.say(env.sid, 'привет', channel='telegram')
            await self.until(lambda: self.sent(env))
            self.assertEqual(self.sent(env)[-1]['text'], '🦊: Ответ хозяину.')
            app.bus.publish({'type': 'notify', 'kind': 'remind', 'agent_id': 'luna', 'text': 'Полей цветы'})
            app.bus.publish({'type': 'notify', 'kind': 'update', 'agent_id': '*', 'text': 'Общее для стаи'})
            await self.until(lambda: len(self.sent(env)) >= 3)
            texts = [m['text'] for m in self.sent(env)]
            self.assertTrue(any(t.startswith('🐙: ⏰ Полей цветы') for t in texts), texts)
            self.assertTrue(any(t.startswith('🔄 Общее для стаи') for t in texts), 'сообщение стаи без знака агента')
            app.settings.set('telegram', {'agent_label': False})
            self.assertEqual(app.telegram.signed(luna, 'x'), 'x')


class CompactInstructionsTest(TempDirCase):
    @async_test
    async def test_owner_instructions_reach_summarizer_and_stay_for_next_compactions(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('memory', {'review': False})
            def fill(start):
                for i in range(start, start + 8):
                    env.app.store.add_message(env.sid, {'role': 'user', 'content': f'вопрос {i}: ' + 'про деплой ' * 10})
                    env.app.store.add_message(env.sid, {'role': 'assistant', 'content': f'ответ {i}: ' + 'подробности ' * 10})
            fill(0)
            env.script([{'text': json.dumps({'summary': 'Пересказ про деплой, первая часть.', 'diary': ''}, ensure_ascii=False)}])
            await compact.manual(env.app, env.sid, 'подробно, и всё про деплой сайта сохрани дословно')
            asked = json.dumps(env.fake.state.requests[-1]['body']['messages'], ensure_ascii=False)
            self.assertIn('Указание хозяина к этому пересказу', asked); self.assertIn('подробно, и всё про деплой сайта сохрани дословно', asked)
            self.assertNotIn('2500', asked, 'ограничения длины нет')
            self.assertEqual(compact.state(env.app, env.sid)['guide'], 'подробно, и всё про деплой сайта сохрани дословно')
            fill(8)
            env.script([{'text': json.dumps({'summary': 'Пересказ, вторая часть.', 'diary': ''}, ensure_ascii=False)}])
            await compact.manual(env.app, env.sid)
            self.assertIn('всё про деплой сайта сохрани дословно', json.dumps(env.fake.state.requests[-1]['body']['messages'], ensure_ascii=False),
                          'указание держится и при следующем сжатии')
