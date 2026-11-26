"""Знак «получил» в Telegram приходит сразу: до очереди чата, расшифровки, решения Предохранителя и записи в журнал."""
import asyncio
import os
from unittest.mock import patch

from bender.core.app import App
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv
from tests import test_telegram as original


class AckTest(TempDirCase):
    _up = original.TelegramTest._up
    push = original.TelegramTest.push
    sent = original.TelegramTest.sent
    until = original.TelegramTest.until

    def typing(self, env):
        return self.sent(env, 'sendChatAction')

    @async_test
    async def test_typing_goes_out_at_receipt_while_the_message_is_still_being_prepared(self):
        """Расшифровка голоса, пачка пересланного или очередь чата — секунды; «печатает…» не ждёт ничего из этого."""
        async with MindEnv(self.tmp) as env:
            await self._up(env, owner=True)
            tg = env.app.telegram
            started, release = asyncio.Event(), asyncio.Event()
            real = tg._handle

            async def slow(u):
                started.set()
                await release.wait()  # разбор сообщения не кончается
                return await real(u)

            env.script([{'text': 'Слышу.'}])
            with patch.object(tg, '_handle', slow):
                self.push(env, 'привет')
                await asyncio.wait_for(started.wait(), 5)
                await self.until(lambda: self.typing(env))
                self.assertEqual(self.typing(env)[0]['action'], 'typing')
                self.assertFalse(self.sent(env), 'ответа ещё нет — только знак «получил»')
                release.set()
            await self.until(lambda: any('Слышу.' in s.get('text', '') for s in self.sent(env)))

    @async_test
    async def test_no_early_typing_for_commands_strangers_groups_and_edits(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env, owner=True)
            base = {'message_id': 7, 'date': 1, 'text': 'привет'}
            self.push(env, '/help')  # команда отвечает сама
            self.push(env, 'чужой', uid=99)
            self.push(env, message={**base, 'from': {'id': 42, 'is_bot': False}, 'chat': {'id': -100, 'type': 'supergroup'}})
            self.push(env, message={**base, 'from': {'id': 42, 'is_bot': True}, 'chat': {'id': 42, 'type': 'private'}})
            self.push(env, edited_message={**base, 'from': {'id': 42, 'is_bot': False}, 'chat': {'id': 42, 'type': 'private'}, 'edit_date': 2})
            await self.until(lambda: len(self.sent(env)) >= 2)  # ответы на /help и чужому
            await asyncio.sleep(0.4)
            self.assertEqual(self.typing(env), [])

    @async_test
    async def test_thread_of_the_message_is_kept(self):
        async with MindEnv(self.tmp) as env:
            env.fake.state.tg_topics = True  # темы лички включены: знак идёт в ту же тему, откуда написал хозяин
            await self._up(env, owner=True)
            env.script([{'text': 'Ок.'}])
            self.push(env, message={'message_id': 8, 'date': 1, 'text': 'в теме', 'message_thread_id': 55, 'from': {'id': 42, 'is_bot': False}, 'chat': {'id': 42, 'type': 'private'}})
            await self.until(lambda: self.typing(env))
            self.assertEqual(self.typing(env)[0].get('message_thread_id'), 55)

    @async_test
    async def test_turn_starts_typing_before_the_guard_decision_is_written(self):
        """Решение Предохранителя пишется в журнал; знак «получил» не должен ждать эту запись."""
        async with MindEnv(self.tmp) as env:
            await self._up(env, owner=True)
            tg, app = env.app.telegram, env.app
            blocked, gate = asyncio.Event(), asyncio.Event()
            check = app.guard.check

            async def slow_check(action, *args, **kwargs):
                if 'Черновик активного хода' in str(getattr(action, 'summary', '')):
                    blocked.set()
                    await gate.wait()
                return await check(action, *args, **kwargs)

            env.script([{'text': 'Готово.'}])
            with patch.object(tg, '_early_ack', lambda u: None), patch.object(app.guard, 'check', slow_check):
                self.push(env, 'привет')
                await asyncio.wait_for(blocked.wait(), 5)
                await self.until(lambda: self.typing(env))
                gate.set()
            await self.until(lambda: any('Готово.' in s.get('text', '') for s in self.sent(env)))


class JournalWriteTest(TempDirCase):
    def test_journal_write_does_not_wait_for_the_disk_twice_on_every_entry(self):
        """Запись журнала идёт в потоке цикла на каждое решение Предохранителя: fsync файла и каталога на SD — сотни мс."""
        app = App(self.tmp / 'data')
        calls = []
        real = os.fsync
        try:
            with patch('os.fsync', lambda fd: (calls.append(fd), real(fd))[1]):
                for i in range(5):
                    app.journal.write('test', f'запись {i}')
            self.assertEqual(calls, [])
            self.assertTrue(app.journal.verify()['ok'])
            seq, _hash = app.journal.anchor.read_text().split()
            self.assertEqual(int(seq), app.store.one('SELECT max(seq) AS n FROM journal')['n'])  # якорь по-прежнему ведёт за хвостом
        finally:
            app.store.close()
