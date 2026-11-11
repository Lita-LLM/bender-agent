"""Перегрузка: цикл отвечает медленно — плановые дела ждут; сумма трат не считается на каждое событие."""
import asyncio
import time

from bender.core.app import App
from bender.core.stall import LoopWatch
from tests.helpers import TempDirCase, async_test


class LoadTest(TempDirCase):
    @async_test
    async def test_loop_watch_measures_how_late_the_loop_answers(self):
        watch = LoopWatch(asyncio.get_running_loop(), lambda record: None, None, stall=60, interval=0.05)
        watch.start()
        try:
            await asyncio.sleep(0.3)
            self.assertLess(watch.lag, 0.3)
            time.sleep(0.6)  # цикл занят и не отвечает на стук
            await asyncio.sleep(0.3)
            self.assertGreaterEqual(watch.lag, 0.4)
            self.assertTrue(watch.busy(0.4))
        finally:
            watch.stop()

    @async_test
    async def test_background_work_waits_while_overloaded_but_not_forever(self):
        app = App(self.tmp / 'data')
        try:
            class Busy:
                lag = 5.0

                def busy(self, limit=1.0):
                    return True
            self.assertFalse(app.agents.overloaded())  # сторожа цикла нет — нечего мерить
            app.loop_watch = Busy()
            self.assertTrue(app.agents.overloaded())
            start = time.monotonic()
            await app.agents.calm(limit=0.3)
            self.assertGreaterEqual(time.monotonic() - start, 0.25)
            app.settings.set('flock', {'shed_load': False})  # хозяин отключил притормаживание
            self.assertFalse(app.agents.overloaded())
            start = time.monotonic()
            async with app.agents.background():
                pass
            self.assertLess(time.monotonic() - start, 0.2)
        finally:
            await app.close()

    @async_test
    async def test_cost_is_cached_briefly_and_fresh_bypasses(self):
        app = App(self.tmp / 'data')
        try:
            self.assertEqual(app.agents.cost('main'), 0.0)
            app.store.x("INSERT INTO calls(id, session_id, created_at, role, provider, model, usage, cost, seconds, notice, request, error) VALUES('c1','s',?,'main','p','m','{}',1.5,1,'','{}','')", (time.time(),))
            self.assertEqual(app.agents.cost('main'), 0.0)  # в окне кэша
            self.assertEqual(app.agents.cost('main', fresh=True), 1.5)
            self.assertEqual(app.agents.total_cost(), 1.5)
        finally:
            await app.close()
