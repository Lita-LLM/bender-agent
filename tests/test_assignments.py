"""Поручения между агентами и вопрос хозяину из письма, когда хозяина нет рядом."""
import asyncio

from bender.core.errors import HumanError
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class AssignmentsTest(TempDirCase):
    def prepare(self, env):
        app = env.app
        app.settings.set('memory', {'review': False, 'compact': False})
        app.agents.create('Второй', agent_id='second', awaken=False, profile='helper')
        return app, ToolContext(app, session_id=env.sid)

    @async_test
    async def test_assignment_lifecycle_and_notices(self):
        async with MindEnv(self.tmp) as env:
            app, ctx = self.prepare(env)
            made = await app.tools.run('assign', {'agent': 'second', 'title': 'Свести отчёт', 'text': 'Собери итоги недели и скажи, что считать готовым.', 'hours': 2}, ctx)
            self.assertTrue(made.ok, made.content)
            ident = made.data['id']
            second = app.agents.get('second')
            mine = await app.tools.run('assignments', {}, ToolContext(second, session_id=second.main_session()))
            self.assertIn('Свести отчёт', mine.content)
            self.assertEqual(app.agents.assignments.counts('second'), (1, 0))
            self.assertEqual(app.agents.assignments.counts('main'), (0, 1))
            with self.assertRaises(HumanError):  # чужое поручение исполнитель не двигает за создателя, создатель — за исполнителя
                app.agents.assignments.update('main', ident, 'done', '')
            done = await app.tools.run('assignment_update', {'id': ident, 'state': 'done', 'note': 'Отчёт в папке work'}, ToolContext(second, session_id=second.main_session()))
            self.assertTrue(done.ok, done.content)
            texts = [m['content'] for m in app.store.messages(app.main_session())]
            self.assertTrue(any('выполнил поручение' in t and 'Отчёт в папке work' in t for t in texts), texts)
            self.assertEqual(app.agents.assignments.counts('second'), (0, 0))
            with self.assertRaises(HumanError):
                app.agents.assignments.update('second', ident, 'taken', '')

    @async_test
    async def test_refusal_needs_reason_and_owner_can_cancel(self):
        async with MindEnv(self.tmp) as env:
            app, ctx = self.prepare(env)
            ident = (await app.tools.run('assign', {'agent': 'second', 'title': 'Проверка', 'text': 'Проверь настройки'}, ctx)).data['id']
            with self.assertRaises(HumanError):
                app.agents.assignments.update('second', ident, 'refused', '')
            row = app.agents.assignments.update('owner', ident, 'cancelled', 'Не нужно')
            self.assertEqual(row['state'], 'cancelled')
            self.assertEqual(app.agents.assignments.listing(active=True), [])

    @async_test
    async def test_runtime_note_mentions_open_assignments(self):
        async with MindEnv(self.tmp) as env:
            app, ctx = self.prepare(env)
            from bender.mind.context import runtime_note
            self.assertNotIn('поручения', runtime_note(app, 'pult'))
            await app.tools.run('assign', {'agent': 'second', 'title': 'Дело', 'text': 'Сделай дело'}, ctx)
            self.assertIn('поручения: тебе 0, от тебя 1', runtime_note(app, 'pult'))

    @async_test
    async def test_ask_owner_from_letter_does_not_wait_and_answer_returns(self):
        async with MindEnv(self.tmp) as env:
            app, _ = self.prepare(env)
            second = app.agents.get('second')
            ctx = ToolContext(second, session_id=second.main_session(), unattended=True, turn={'agent_letter': 'нет-такого'})
            result = await asyncio.wait_for(second.tools.run('ask_owner', {'question': 'Выбрать зелёный или синий?', 'options': ['зелёный', 'синий']}, ctx), 5)
            self.assertTrue(result.ok, result.content)
            self.assertIn('ждать не нужно', result.content)
            await asyncio.sleep(0.2)
            cards = list(app.questions.values())
            self.assertEqual(len(cards), 1)
            self.assertIn('«Второй» спрашивает', cards[0]['card']['question'])
            self.assertTrue(app.answer(cards[0]['card']['id'], 'синий'))
            await asyncio.sleep(0.5)
            texts = [str(m['content']) for m in second.store.messages(second.main_session())]
            self.assertTrue(any('Ответ хозяина' in t and 'синий' in t for t in texts), texts)
