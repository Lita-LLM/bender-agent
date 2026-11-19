"""Долгая цель: ход не кончается на ответе без тулов, пока цель открыта; блок ждёт хозяина; пределы."""
from bender.mind import goals
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


class GoalTest(TempDirCase):
    @async_test
    async def test_open_goal_continues_turn_until_done(self):
        async with MindEnv(self.tmp) as env:
            app = env.app; app.selfdir.finish_awakening()
            env.script([call('goal', action='set', text='Починить сборку', done_when='тесты зелёные'),
                        {'text': 'Первый шаг сделал, продолжать?'},
                        call('plan', items=['шаг 2']),
                        call('goal', action='done', note='тесты зелёные'),
                        {'text': 'Готово: сборка чинится, тесты зелёные.'}])
            res = await app.mind.say(env.sid, 'Доведи сборку до зелёных тестов')
            self.assertIsNone(res['error'], res)
            self.assertEqual(res['text'], 'Готово: сборка чинится, тесты зелёные.')
            self.assertEqual(goals.get(app, env.sid)['state'], 'done')
            nudges = [m for m in app.store.messages(env.sid, limit=50) if (m.get('meta') or {}).get('goal')]
            self.assertEqual(len(nudges), 1)
            self.assertIn('Починить сборку', str(nudges[0]['content']))

    @async_test
    async def test_idle_answers_pause_goal_and_owner_word_wakes_it(self):
        async with MindEnv(self.tmp) as env:
            app = env.app; app.selfdir.finish_awakening()
            env.script([call('goal', action='set', text='Собрать отчёт'),
                        {'text': 'Думаю'}, {'text': 'Всё ещё думаю'}, {'text': 'Не знаю, что дальше'}])
            res = await app.mind.say(env.sid, 'Сделай отчёт')
            self.assertEqual(res['text'], 'Не знаю, что дальше')
            goal = goals.get(app, env.sid)
            self.assertEqual(goal['state'], 'paused')
            self.assertIn('ЦЕЛЬ (на паузе): Собрать отчёт', goals.line(app, env.sid))
            env.script([call('goal', action='blocked', note='Какой формат: PDF или таблица?'), {'text': 'PDF или таблица?'}])
            await app.mind.say(env.sid, 'Продолжай')
            self.assertEqual(goals.get(app, env.sid)['state'], 'blocked')
            goals.wake(app, env.sid)
            self.assertEqual(goals.get(app, env.sid)['state'], 'active')

    @async_test
    async def test_rounds_limit_and_zero_disables(self):
        async with MindEnv(self.tmp) as env:
            app = env.app; app.selfdir.finish_awakening()
            app.settings.set('agent', {'goal_rounds': 2})
            env.script([call('goal', action='set', text='Бесконечная работа')]
                       + [x for i in range(3) for x in (call('plan', items=[str(i)]), {'text': 'шаг ' + str(i)})])
            res = await app.mind.say(env.sid, 'Работай')
            self.assertEqual(res['text'], 'шаг 2')
            self.assertEqual(goals.get(app, env.sid)['state'], 'paused')
            self.assertIn('2', goals.get(app, env.sid)['note'])
            app.settings.set('agent', {'goal_rounds': 0})
            env.script([call('goal', action='set', text='Новая'), {'text': 'Сразу ответ'}])
            res = await app.mind.say(env.sid, 'Ещё')
            self.assertEqual(res['text'], 'Сразу ответ')
            self.assertEqual(goals.get(app, env.sid)['state'], 'active')

    def test_agent_cannot_raise_rounds_alone(self):
        from bender.core.settings import SCHEMA
        field = SCHEMA['agent'].field('goal_rounds')
        self.assertTrue(field.weakens(30, 60))
        self.assertFalse(field.weakens(30, 10))
