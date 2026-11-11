"""`bender usage`: расход по часам и видам ходов — только числа, без текстов и названий чатов."""
from bender.diagnostics.usage import report
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class UsageReportTest(TempDirCase):
    @async_test
    async def test_counts_kinds_without_private_text(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.settings.set('memory', {'review': False, 'compact': False})
            app.agents.create('Второй', agent_id='second', awaken=False, profile='helper')
            env.script([{'text': 'Секретный ответ хозяину'}])
            await app.mind.say(env.sid, 'Тайный вопрос про пароль')
            app.store.update_session(env.sid, title='Личное название чата')
            env.script([{'text': 'Ответ второго'}])
            result = await app.tools.run('message_agent', {'agent': 'second', 'text': 'Личное письмо', 'wait': True}, ToolContext(app, session_id=env.sid))
            self.assertTrue(result.ok, result.content)
            text = report(app.dirs.root, hours=1)
        for private in ('Секретный', 'Тайный', 'пароль', 'Личное', 'Ответ второго'):
            self.assertNotIn(private, text)
        self.assertIn('переписка стаи', text)
        self.assertIn('second', text)
        self.assertIn('Письма стаи по часам', text)
        self.assertIn('main → second: 1', text)
        self.assertIn('Итого: вызовов 2', text)

    def test_missing_database_is_human_error(self):
        from bender.core.errors import HumanError
        with self.assertRaises(HumanError):
            report(self.tmp / 'нет', hours=1)
