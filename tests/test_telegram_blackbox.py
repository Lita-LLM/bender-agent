"""Чёрный ящик Telegram: каждое опоздавшее сообщение объясняет в журнале, где оно ждало."""
import asyncio
import time
from unittest.mock import patch

from bender.channels import telegram as tgmod
from bender.channels.telegram import TelegramError
from bender.core.stall import LoopWatch
from bender.tools.registry import ToolResult
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call
from tests import test_telegram as original


class BlackBoxTest(TempDirCase):
    _up = original.TelegramTest._up
    push = original.TelegramTest.push
    sent = original.TelegramTest.sent
    until = original.TelegramTest.until

    def late(self, env, minutes, text='старое'):
        """Сообщение, отправленное хозяином minutes минут назад."""
        st = env.fake.state
        self.push(env, message={'message_id': 1000 + st.tg_next_id, 'from': {'id': 42, 'first_name': 'Хозяин'},
                                'chat': {'id': 42, 'type': 'private'}, 'date': int(time.time() - minutes * 60), 'text': text})

    def journal(self, app, needle):
        return [r['summary'] for r in app.journal.recent(kind='channel') if needle in r['summary']]

    @async_test
    async def test_late_message_while_polling_blames_telegram_side(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env, owner=True)
            await self.until(lambda: app.telegram.polled_at)
            env.script([{'text': 'Ответ.'}])
            self.late(env, 5)
            await self.until(lambda: self.journal(app, 'пришло через'))
            line = self.journal(app, 'пришло через')[0]
            self.assertIn('опрос шёл исправно', line)
            await self.until(lambda: not app.telegram._bg)

    @async_test
    async def test_outage_is_logged_backoff_is_short_and_late_message_names_it(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env, owner=True)
            tg = app.telegram
            await self.until(lambda: tg.polled_at)
            api, failures, waits = tg.api, [], []

            async def conflict(method, payload=None, **kw):
                if method == 'getUpdates' and len(failures) < 3:
                    failures.append(method)
                    raise TelegramError('tg_api', 'Telegram: Conflict: terminated by other getUpdates request', method, data={'code': 409, 'retry_after': None})
                return await api(method, payload, **kw)

            real_sleep = asyncio.sleep

            async def quick(seconds, *a, **kw):  # паузы между попытками — записываем, но не ждём
                if seconds >= 1: waits.append(seconds); seconds = 0
                return await real_sleep(seconds, *a, **kw)

            env.script([{'text': 'Ответ.'}])
            await tg.stop()  # дальше опрос идёт только через подменённый Telegram
            with patch.object(tg, 'api', conflict), patch.object(tgmod.asyncio, 'sleep', quick), patch.object(tgmod, 'POLL_GAP', 0.0):
                # Хозяин написал 2 минуты назад, пока бот был отрезан.
                self.late(env, 2)
                tg.start()
                await self.until(lambda: self.journal(app, 'пришло через'))
            self.assertEqual(len(failures), 3)
            self.assertTrue(self.journal(app, 'кто-то ещё'), 'обрыв назван: второй опрашивающий с тем же токеном')
            self.assertTrue(self.journal(app, 'связь вернулась'))
            self.assertIn('связи с Telegram не было', self.journal(app, 'пришло через')[0])
            self.assertEqual([w for w in waits if w >= 5], [5.0, 5.0, 5.0])  # getMe проходит — пауза не растёт (4,5 с — чужие паузы)
            self.assertIsNone(tg.public()['outage'])
            await self.until(lambda: not tg._bg)

    @async_test
    async def test_backoff_never_exceeds_thirty_seconds_on_network_errors(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env, owner=True)
            tg = app.telegram
            waits = []

            async def down(method, payload=None, **kw):
                raise TelegramError('tg_net', 'Нет связи с Telegram', 'api.telegram.org не ответил за 65 с')

            real_sleep = asyncio.sleep

            async def quick(seconds, *a, **kw):
                if seconds >= 1: waits.append(seconds); seconds = 0
                return await real_sleep(seconds, *a, **kw)

            with patch.object(tg, 'api', down), patch.object(tgmod.asyncio, 'sleep', quick):
                await self.until(lambda: len(waits) >= 8)
                self.assertEqual(tg.public()['outage']['reason'], 'Нет связи с Telegram: api.telegram.org не ответил за 65 с')
                await tg.stop()
            self.assertEqual(max(waits), tgmod.NET_RETRY_MAX)
            self.assertTrue(self.journal(app, 'нет связи'))

    @async_test
    async def test_message_into_running_turn_gets_eyes_and_wait_is_explained(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env, owner=True)
            tg = app.telegram
            entered, release = asyncio.Event(), asyncio.Event()

            @app.tools.tool('event_wait', 'Подождать', group='base')
            async def waiting(ctx):
                entered.set(); await release.wait(); return ToolResult('Готово')

            env.script([call('event_wait'), {'text': 'Сделал и прочитал твоё.'}])
            with patch.object(tgmod, 'LATE', 0.2):
                self.push(env, 'долгая задача')
                await asyncio.wait_for(entered.wait(), 5)
                self.push(env, 'ты тут?')
                await self.until(lambda: any(r.get('reaction') == [{'type': 'emoji', 'emoji': '👀'}] for r in self.sent(env, 'setMessageReaction')))
                await asyncio.sleep(0.3)
                release.set()
                await self.until(lambda: self.journal(app, 'ждало конца идущего хода'))
            line = self.journal(app, 'ждало конца идущего хода')[0]
            self.assertIn('выполняет тул event_wait', line)
            await self.until(lambda: not tg._bg)


class LoopWatchTest(TempDirCase):
    @async_test
    async def test_blocked_loop_is_reported_with_place(self):
        loop = asyncio.get_running_loop()
        found = []
        watch = LoopWatch(loop, found.append, self.tmp / 'stalls.txt', stall=0.3, interval=0.05)
        watch.start()
        try:
            await asyncio.sleep(0.2)
            time.sleep(1.0)  # блокирующий вызов прямо в цикле — как замок базы, занятый потоком
            for _ in range(40):
                if found: break
                await asyncio.sleep(0.05)
        finally:
            watch.stop()
        self.assertEqual(len(found), 1)
        self.assertIn('test_blocked_loop_is_reported_with_place', found[0]['where'])
        self.assertGreater(found[0]['seconds'], 0.5)
        self.assertIn('цикл стоял', (self.tmp / 'stalls.txt').read_text(encoding='utf-8'))
        self.assertEqual(watch.between(time.time() - 10, time.time()), found)

    def test_dead_loop_dumps_stacks_and_exits(self):
        # Цикл не ожил совсем (регулярное выражение держит GIL): процесс сам пишет стеки и выходит — Сторож поднимет заново.
        import os, subprocess, sys
        from pathlib import Path
        from bender.core.stall import hang_place
        code = ("import asyncio,re,sys\nfrom pathlib import Path\nfrom bender.core.stall import LoopWatch\n"
                "def catastrophe():\n    re.match(r'(a+)+$','a'*60+'b')\n"
                "async def main():\n    w=LoopWatch(asyncio.get_running_loop(),print,Path(sys.argv[1])/'stalls.txt')\n    w.start()\n    await asyncio.sleep(0.2)\n    catastrophe()\n"
                "asyncio.run(main())\n")
        root = str(Path(__file__).resolve().parents[1])
        done = subprocess.run([sys.executable, '-c', code, str(self.tmp)], env={**os.environ, 'PYTHONPATH': root, 'BENDER_HANG_SECONDS': '2'}, timeout=60, capture_output=True)
        self.assertEqual(done.returncode, 1)
        text = (self.tmp / 'hang.txt').read_text(encoding='utf-8')
        self.assertIn('catastrophe', text)
        watch = LoopWatch(None, None, self.tmp / 'stalls.txt')
        self.assertIn('catastrophe', watch.previous_hang())
        self.assertFalse((self.tmp / 'hang.txt').exists())
        self.assertTrue((self.tmp / 'hang-last.txt').exists())
        self.assertEqual(watch.previous_hang(), '')
        self.assertEqual(hang_place('Timeout (0:03:00)!\nThread 0x1 (most recent call first):\n  File "/x/threading.py", line 1 in wait\n\nThread 0x2 (most recent call first):\n'
                                    '  File "/usr/lib/re/__init__.py", line 167 in match\n  File "/opt/bender/channels/telegram_rich.py", line 30 in _inline\n  File "/usr/lib/asyncio/base_events.py", line 2061 in _run_once\n'),
                         'telegram_rich.py:30 _inline')

