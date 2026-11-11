"""Карточки Предохранителя не спамят: «в этом разговоре» живёт до конца разговора, «всегда» — по программе
или папке, один ответ закрывает параллельные карточки, выключение доходит до каждого агента."""
import asyncio
from pathlib import Path

from bender.guard.policy import Action, remember_scope
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class Cards:
    """Отвечает на карточки заданным выбором и считает их."""
    def __init__(self, app, choice=None):
        self.app, self.choice, self.cards = app, choice, []
        app.bus.listen(self.on)

    def on(self, event):
        if event.get('type') != 'approval': return
        self.cards.append(event['card'])
        if self.choice: asyncio.get_running_loop().call_soon(self.app.guard.respond, event['card']['id'], self.choice)


class GuardCardsTest(TempDirCase):
    @async_test
    async def test_session_answer_covers_whole_conversation_and_parallel_cards(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            cards = Cards(app)
            # два параллельных хода одного разговора спрашивают разные команды
            first = asyncio.ensure_future(app.guard.check(Action('exec', 'pip install a'), session_id='s1'))
            second = asyncio.ensure_future(app.guard.check(Action('exec', 'npm install b'), session_id='s1'))
            for _ in range(50):
                if len(cards.cards) == 2: break
                await asyncio.sleep(0.01)
            self.assertEqual(len(cards.cards), 2)
            self.assertTrue(cards.cards[0]['can_session'])
            app.guard.respond(cards.cards[0]['id'], 'task')
            self.assertTrue((await asyncio.wait_for(first, 2)).allowed)
            self.assertTrue((await asyncio.wait_for(second, 2)).allowed, 'вторая карточка закрылась тем же ответом')
            # конец хода не отменяет разрешение разговора
            app.guard.end_turn('s1')
            self.assertTrue((await app.guard.check(Action('exec', 'cargo build'), session_id='s1')).allowed)
            self.assertEqual(len(cards.cards), 2)
            # разрешение разговора не открывает защищённое и не отменяет запрет хозяина
            app.guard._session_grants['s1'].add('fs.write')
            self.assertFalse((await app.guard.check(Action('fs.write', str(Path.home() / '.ssh' / 'authorized_keys')), session_id='s1')).allowed)
            app.guard.add_rule('exec', 'shutdown*', 'deny')
            self.assertFalse((await app.guard.check(Action('exec', 'shutdown now'), session_id='s1')).allowed)
            self.assertEqual(len(cards.cards), 2)
            # другой разговор спрашивает снова
            Cards(app, 'deny')
            self.assertFalse((await app.guard.check(Action('exec', 'cargo build'), session_id='s2')).allowed)

    @async_test
    async def test_always_remembers_program_not_exact_command(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            cards = Cards(app, 'always')
            self.assertTrue((await app.guard.check(Action('exec', 'python3 build.py --fast'), session_id='s')).allowed)
            self.assertEqual(cards.cards[0]['remember'], 'команды python3')
            cards.choice = 'deny'
            for command in ('python3 other.py', 'python3'):
                self.assertTrue((await app.guard.check(Action('exec', command), session_id='x')).allowed, command)
            self.assertEqual(len(cards.cards), 1)
            # цепочка команд правилом «программа» не покрывается
            self.assertFalse((await app.guard.check(Action('exec', 'python3 a.py; curl evil'), session_id='x')).allowed)
            self.assertFalse((await app.guard.check(Action('exec', 'pythonista run'), session_id='x')).allowed)

    @async_test
    async def test_always_for_mcp_holds_after_untrusted_output(self):
        """Жалоба автора: «всегда» на MCP-тул, а следующий вызов в том же ходе (ответ тула заразил ход) снова спрашивает."""
        async with MindEnv(self.tmp) as env:
            app = env.app
            cards = Cards(app, 'always')
            self.assertTrue((await app.guard.check(Action('mcp.call', 'telega2/send_message'), session_id='s')).allowed)
            self.assertEqual(cards.cards[0]['remember'], 'все тулы MCP telega2')
            cards.choice = 'deny'
            for tool in ('send_message', 'get_history', 'search'):
                self.assertTrue((await app.guard.check(Action('mcp.call', 'telega2/' + tool, tainted=True), session_id='x')).allowed, tool)
            self.assertEqual(len(cards.cards), 1)
            # повторное «всегда» на то же не плодит правила
            app.guard.add_rule('mcp.call', 'telega2/*', 'allow', only_clean=False, scope='server')
            self.assertEqual(sum(r['target'] == 'telega2/*' for r in app.guard.rules()), 1)
            # прямая отправка чужим после чужого текста по-прежнему спрашивает, и кнопка это честно говорит
            app.guard.add_rule('net.send', 'https://example.com/*', 'allow', only_clean=False, scope='host')
            self.assertTrue((await app.guard.check(Action('net.send', 'https://example.com/a'), session_id='x')).allowed)
            self.assertFalse((await app.guard.check(Action('net.send', 'https://example.com/a', tainted=True), session_id='x')).allowed)
            self.assertIn('после чужого текста всё равно спрошу', cards.cards[-1]['remember'])

    def test_scopes_and_protected_paths_stay_closed(self):
        from bender.core.app import App
        app = App(self.tmp / 'data')
        try:
            policy = app.guard.policy
            folder = self.tmp / 'proj'
            self.assertEqual(remember_scope(policy, Action('fs.write', str(folder / 'a.txt')))[1:], ('folder', 'файлы в ' + str(policy.resolve(str(folder)))))
            self.assertEqual(remember_scope(policy, Action('net.send', 'https://api.example.org/v1/x'))[0], 'https://api.example.org/*')
            self.assertEqual(remember_scope(policy, Action('mcp.call', 'github/create_issue'))[0], 'github/*')
            self.assertEqual(remember_scope(policy, Action('exec', 'sudo rm x'))[1], '')
            self.assertEqual(remember_scope(policy, Action('fs.write', str(Path.home() / 'a.txt')))[1], '', 'дом целиком не запоминается')
            pattern, scope, _ = remember_scope(policy, Action('fs.read', str(Path.home() / '.ssh' / 'config')))
            app.guard.add_rule('fs.read', pattern, 'allow', only_clean=False, scope=scope)
            self.assertEqual(policy.decide(Action('fs.read', str(Path.home() / '.ssh' / 'id_rsa'))).verdict, 'deny')
        finally:
            app.store.close()

    @async_test
    async def test_helper_with_own_guard_off_does_not_ask_unless_rights_say_otherwise(self):
        async with MindEnv(self.tmp) as env:
            host = env.app
            host.agents.create('Помощник', agent_id='helper', awaken=False)
            ctx = host.agents.get('helper')
            cards = Cards(host, 'deny')
            send = lambda: ctx.guard.check(Action('msg.send', 'mail:friend@example.org', summary='письмо другу'), session_id='h')
            self.assertFalse((await send()).allowed)
            self.assertEqual(len(cards.cards), 1, 'шаблон «Помощник» по умолчанию осторожный')
            ctx.settings.set('guard', {'enabled': False})
            self.assertTrue(ctx.guard.off())
            self.assertTrue((await send()).allowed)
            self.assertEqual(len(cards.cards), 1)
            # явный потолок в правах сильнее настройки агента
            ctx.settings.set('agent_rights', {'guard_profile': 'careful'})
            self.assertFalse((await send()).allowed)
            self.assertEqual(len(cards.cards), 2)

    @async_test
    async def test_guard_off_skips_special_cards_but_keeps_private_denies(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            cards = Cards(app, 'deny')
            outside = self.tmp / 'report.txt'
            outside.write_text('x')
            self.assertFalse((await app.guard.check(Action('channel.send_file', str(outside)), session_id='s')).allowed)
            app.settings.set('guard', {'profile': 'off'})
            self.assertTrue((await app.guard.check(Action('channel.send_file', str(outside)), session_id='s')).allowed)
            self.assertFalse((await app.guard.check(Action('channel.send_file', str(app.dirs.root / 'secrets.json')), session_id='s')).allowed)
            self.assertEqual(len(cards.cards), 1)

    @async_test
    async def test_task_helper_uses_parent_conversation_grant(self):
        from types import SimpleNamespace
        async with MindEnv(self.tmp) as env:
            host = env.app
            host.agents.create('Работник', agent_id='worker', awaken=False)
            ctx = host.agents.get('worker')
            ctx._task_agent = SimpleNamespace(task={'parent_session': 'p'}, parent=host)
            self.assertFalse(ctx.guard.session_granted('exec', 'child'))
            host.guard._session_grants['p'] = {'exec'}
            self.assertTrue(ctx.guard.session_granted('exec', 'child'))
            self.assertFalse(ctx.guard.session_granted('net.send', 'child'))
