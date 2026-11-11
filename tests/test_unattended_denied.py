"""Ход без хозяина: отказ Предохранителя доходит до хозяина строкой, а не только до модели."""
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


class UnattendedDeniedTest(TempDirCase):
    @async_test
    async def test_scheduled_turn_reports_what_was_denied(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            seen = []
            app.bus.listen(lambda ev: seen.append(ev) if ev.get('type') == 'notify' else None)
            env.script([call('write_file', path='/etc/bender-denied-check', content='x'), {'text': 'Готово, всё опубликовал.'}])
            result = await app.mind.say(env.sid, 'опубликуй пост', channel='cron', actor='schedule', unattended=True)
            await app.mind.idle(5)
            self.assertEqual(result['denied'][0]['tool'], 'write_file')
            notice = next(ev for ev in seen if ev.get('kind') == 'denied')
            self.assertIn('по расписанию', notice['text'])
            self.assertIn('write_file', notice['text'])
            self.assertIn('не выполнена', notice['text'])
            events = [m for m in app.store.messages(env.sid) if m.get('role') == 'event' and m.get('kind') == 'denied']
            self.assertEqual(len(events), 1)

    @async_test
    async def test_owner_turn_has_no_extra_notice(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            seen = []
            app.bus.listen(lambda ev: seen.append(ev) if ev.get('type') == 'notify' and ev.get('kind') == 'denied' else None)
            env.auto_approve('deny')
            env.script([call('write_file', path='/etc/bender-denied-check', content='x'), {'text': 'Не вышло.'}])
            result = await app.mind.say(env.sid, 'запиши')
            await app.mind.idle(5)
            self.assertNotIn('denied', result)
            self.assertEqual(seen, [])
