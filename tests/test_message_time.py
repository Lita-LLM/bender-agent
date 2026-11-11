"""Время сообщений стабильно в истории и сохраняется при пересказе."""
import datetime as dt
import json
from unittest.mock import patch

from bender.core.dates import stamp, prefix, without_prefix
from bender.memory.index import Hit, format_hits
from bender.mind import compact, side
from bender.mind.loop import Turn
from bender.mind.passport import messages
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class MessageTimeTest(TempDirCase):
    def test_unrepresentable_old_timestamp_does_not_break_history(self):
        self.assertEqual(stamp(1e100), '[время не задано]')
        self.assertNotIn('прошло', prefix(0, float('-inf')))
        self.assertEqual(without_prefix('[время не задано]\nтекст'), 'текст')

    @async_test
    async def test_frozen_owner_internal_gap_and_identical_history(self):
        async with MindEnv(self.tmp) as env:
            app = env.app; base = dt.datetime(2026, 10, 23, 15, 54, tzinfo=dt.timezone.utc).timestamp()
            with patch('bender.core.dates.local', side_effect=lambda ts=None: dt.datetime.fromtimestamp(base if ts is None else ts, dt.timezone.utc)):
                with patch('bender.core.store.time.time', return_value=base):
                    app.store.add_message(env.sid, {'role': 'user', 'content': 'Первое'}, {'actor': 'owner', 'channel': 'telegram'})
                    app.store.add_message(env.sid, {'role': 'assistant', 'content': 'Ответ'})
                with patch('bender.core.store.time.time', return_value=base + 3 * 3600):
                    app.store.add_message(env.sid, {'role': 'user', 'content': 'Задача готова'}, {'actor': 'team', 'channel': 'team'})
                first = compact.window(app, env.sid, 10000).messages
                second = compact.window(app, env.sid, 10000).messages
                self.assertEqual(json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))
                self.assertEqual(first[0]['content'], '[пт 23.10 15:54]\nПервое')
                self.assertEqual(first[1]['content'], 'Ответ')
                self.assertIn('(прошло 3 ч)', first[2]['content'])
                self.assertEqual(app.store.messages(env.sid)[0]['content'], 'Первое')
                transcript = side.transcript(app.store.messages(env.sid))
                self.assertIn('[пт 23.10 15:54] Хозяин: Первое', transcript)
                hit = Hit(1, 'session', env.sid, '1', 'Первое', base, 'owner', 5, 1)
                self.assertIn('пт 23.10 15:54', format_hits([hit]))
                app.store.x("UPDATE messages SET meta='{}' WHERE session_id=?", (env.sid,))
                old = compact.window(app, env.sid, 10000).messages
                self.assertEqual(old, first)  # старый формат без новых полей

    @async_test
    async def test_clock_in_volatile_passport_keeps_cached_prefix_and_model_blocks(self):
        async with MindEnv(self.tmp) as env:
            turn = Turn(env.sid, 'pult'); candidate = env.app.router.chain('main')[0]
            source = [{'role': 'system', 'content': 'Правила'}, {'role': 'user', 'content': 'Вопрос'}]
            with patch('bender.core.dates.current', return_value={'date_time': '23.10.2026 15:54', 'timezone': 'UTC+03:00'}):
                first = messages(env.app, turn, candidate, source)
            with patch('bender.core.dates.current', return_value={'date_time': '23.10.2026 18:54', 'timezone': 'UTC+03:00'}):
                self.assertEqual(first, messages(env.app, turn, candidate, source))
                next_turn = messages(env.app, Turn(env.sid, 'pult'), candidate, source)
            self.assertEqual(first[0], next_turn[0])
            self.assertIn('23.10.2026 18:54', next_turn[-1]['content'])
            row = {'role': 'assistant', 'content': '', 'provider_id': 'p', 'provider_model': 'm', 'provider_blocks': [{'type': 'reasoning', 'encrypted_content': 'block'}]}
            self.assertEqual(compact._shape(row)['provider_model'], 'm')

    @async_test
    async def test_compaction_records_period_even_if_summary_omits_dates(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            for i in range(8):
                app.store.add_message(env.sid, {'role': 'user', 'content': 'Событие ' + str(i) + ' ' + 'текст ' * 100})
                app.store.add_message(env.sid, {'role': 'assistant', 'content': 'Ответ ' * 100})
            with patch('bender.mind.side.ask', return_value='{"summary":"Обсудили события.","diary":""}'):
                state = await compact.maybe_compact(app, env.sid, 1500, force=True)
            self.assertIsNotNone(state)
            self.assertIn('[Период:', state['summary'])
            self.assertIn(dt.datetime.now().strftime('%d.%m.%Y'), state['summary'])
            self.assertIn('Обсудили события', state['summary'])


class CopiedLabelsTest(TempDirCase):
    def test_model_does_not_see_or_send_its_own_labels(self):
        from bender.core.dates import content, strip_labels
        self.assertEqual(strip_labels('[пн 26.10 17:11] [#10126] Попробовала снова.'), 'Попробовала снова.')
        self.assertEqual(strip_labels('[#10126]\nГотово'), 'Готово')
        self.assertEqual(strip_labels('[#10126]'), '[#10126]')  # кроме метки нечего отправить — не трогаем
        self.assertEqual(strip_labels('Смотри [#5] выше'), 'Смотри [#5] выше')
        mine = {'role': 'assistant', 'content': '[пн 26.10 17:11] [#9] Ответ', 'created_at': 1e9, 'meta': {'telegram_message_id': 10}}
        # Свои ответы — только с номером, без времени, и без ранее скопированных меток.
        self.assertEqual(content(mine), '[#10]\nОтвет')
        owner = {'role': 'user', 'content': 'Вопрос', 'created_at': 1e9, 'meta': {'telegram_message_id': 11}}
        self.assertRegex(content(owner), r'^\[(пн|вт|ср|чт|пт|сб|вс) .*\] \[#11\]\nВопрос$')
