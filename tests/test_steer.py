"""Новое сообщение хозяина во время хода: вставить в ход, прервать, после хода (agent.new_message).

Фальшивая модель; ждущие тулы — настоящая команда sleep, карточка Предохранителя, вопрос хозяину;
CLI-движок — фальшивый claude; Telegram — фальшивый Bot API."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from bender.core.settings import SCHEMA
from bender.mcp.server import Binding
from bender.mind import steer
from bender.net.http_server import HttpServer
from bender.router import cli as clipkg
from bender.tools.registry import ToolContext, ToolResult
from tests.helpers import TempDirCase, async_test, owner_headers, owner_web
from tests.test_mind import MindEnv, call
from tests import test_telegram as original

WORD = 'СЛОВО-ХОЗЯИНА стой, сделай иначе'
FAKE_CLAUDE = Path(__file__).parent / 'fixtures' / 'fake_claude.py'


def requests(env):
    return [r for r in env.fake.state.requests if r.get('api') == 'openai']


def seen(env):
    """Номер первого запроса к модели, в котором есть слово хозяина (или None)."""
    return next((i for i, r in enumerate(requests(env)) if WORD in json.dumps(r['body'], ensure_ascii=False)), None)


def pairs_ok(rows):
    """Каждый вызов тула ассистента закрыт своим результатом сразу после него — как требуют провайдеры."""
    for i, m in enumerate(rows):
        if m['role'] == 'assistant' and m.get('tool_calls'):
            ids = [c['id'] for c in m['tool_calls']]
            after = [r.get('tool_call_id') for r in rows[i + 1:i + 1 + len(ids)]]
            if after != ids:
                return False
    return True


async def until(cond, timeout=10.0):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError('не дождался')


class SteerTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.events = []

    def listen(self, app):
        app.bus.listen(lambda ev: self.events.append(ev))

    def kinds(self, kind):
        return [e for e in self.events if e.get('type') == kind]

    @async_test
    async def test_setting_schema_default_and_settings_tool(self):
        field = SCHEMA['agent'].field('new_message')
        self.assertEqual((field.type, field.default), ('choice', 'insert'))
        self.assertEqual(list(field.choices), list(steer.MODES))
        self.assertEqual(field.choices['queue'], 'После хода')
        async with MindEnv(self.tmp) as env:
            app = env.app
            described = {f['key']: f for g in app.settings.describe() if g['name'] == 'agent' for f in g['fields']}
            self.assertEqual(described['new_message']['value'], 'insert')  # видна в пульте → Настройки → Агент
            res = await app.tools.run('settings', {'group': 'agent', 'changes': {'new_message': 'queue'}}, ToolContext(app, session_id=env.sid, actor='owner'))
            self.assertTrue(res.ok, res.content)
            self.assertEqual(steer.default(app), 'queue')
            self.assertEqual(steer.normalize('после хода'), 'queue')
            self.assertEqual(steer.normalize('прервать'), 'interrupt')
            self.assertIsNone(steer.normalize('как-нибудь'))

    @async_test
    async def test_insert_via_pult_backgrounds_waiting_command_and_skips_rest_of_packet(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            env.auto_approve('once')
            self.listen(app)
            outside = app.dirs.work / 'не-писать.txt'
            env.script([{'tool_calls': [{'name': 'run', 'arguments': {'command': 'sleep 8; echo поздно'}},
                                        {'name': 'write_file', 'arguments': {'path': str(outside), 'content': 'x'}}]},
                        {'text': 'Понял, делаю иначе.'}])
            web = HttpServer(owner_web(app, allowed_hosts={'127.0.0.1'}))
            await web.start('127.0.0.1', 0)
            try:
                started = time.monotonic()
                turn = asyncio.ensure_future(app.mind.say(env.sid, 'долгая задача'))
                await until(lambda: any(e.get('type') == 'tool_start' and e.get('name') == 'run' for e in self.events))
                await asyncio.sleep(0.3)
                async with await app.http.post(f'http://127.0.0.1:{web.port}/api/say', json={'session': env.sid, 'text': WORD},
                                               headers=owner_headers(**{'X-Bender': '1'})) as response:
                    self.assertEqual(response.status, 200)
                res = await asyncio.wait_for(turn, 6)
            finally:
                await web.stop()
            self.assertLess(time.monotonic() - started, 5)  # не ждал восьмисекундную команду
            self.assertEqual(res['text'], 'Понял, делаю иначе.')
            self.assertEqual(seen(env), 1)  # сразу следующий запрос
            rows = [m for m in app.store.messages(env.sid) if m['role'] != 'event']
            self.assertTrue(pairs_ok(rows))
            tool = [m for m in rows if m['role'] == 'tool']
            self.assertIn('команда НЕ остановлена', tool[0]['content'])
            process = tool[0]['meta']['data']['process']
            self.assertIsNone(app.processes.items[process]['process'].returncode)  # работает в фоне, не убита
            self.assertIn('Не выполнено', tool[1]['content'])  # не начатый вызов пакета
            self.assertFalse(outside.exists())
            word = next(m for m in rows if m['role'] == 'user' and WORD in str(m['content']))
            self.assertTrue(word['meta']['mid_turn'])
            self.assertEqual(word['meta']['delivery'], 'insert')
            last = requests(env)[1]['body']['messages']
            self.assertIn(steer.MARK, json.dumps(last, ensure_ascii=False))  # модель знает, что это пришло посреди хода
            delivered = self.kinds('message_delivered')
            self.assertEqual([(d['delivery'], d['mid_turn']) for d in delivered], [('insert', True)])
            self.assertFalse(app.mind.turns.get(env.sid))
            await app.processes.close()

    @async_test
    async def test_insert_plain_tool_chain_lands_at_next_step(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            self.listen(app)

            @app.tools.tool('quick_step', 'Короткий шаг', group='base')
            async def quick(ctx):
                await asyncio.sleep(0.15)
                return ToolResult('ok')
            env.script([call('quick_step') for _ in range(6)] + [{'text': 'Готово.'}])
            turn = asyncio.ensure_future(app.mind.say(env.sid, 'шаги'))
            await until(lambda: len(self.kinds('tool_end')) >= 2)
            steps_before = len(requests(env))
            await app.mind.say(env.sid, WORD, wait=False)
            await asyncio.wait_for(turn, 10)
            # следующий запрос после текущего пакета (идущий запрос мог уже уйти), а не конец хода
            self.assertLessEqual(seen(env), steps_before + 1)
            self.assertLess(seen(env), len(requests(env)) - 2)

    @async_test
    async def test_insert_closes_open_approval_card_and_question(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            self.listen(app)
            outside = self.tmp / 'вне.txt'
            env.script([call('write_file', path=str(outside), content='x'), {'text': 'Прочитал.'},
                        call('ask_owner', question='Какой цвет?', options=['красный', 'синий']), {'text': 'Ладно.'}])
            turn = asyncio.ensure_future(app.mind.say(env.sid, 'запиши'))
            await until(lambda: app.guard.pending)
            await app.mind.say(env.sid, WORD, wait=False)
            await asyncio.wait_for(turn, 5)
            self.assertFalse(outside.exists())
            done = self.kinds('approval_done')
            self.assertEqual(done[-1]['choice'], 'steer')
            tool = [m for m in app.store.messages(env.sid) if m['role'] == 'tool'][-1]
            self.assertIn('Карточка закрыта без ответа', tool['content'])
            turn_row = app.journal.recent(kind='turn')[0]
            data = turn_row['data'] if isinstance(turn_row['data'], dict) else json.loads(turn_row['data'] or '{}')
            self.assertFalse(data['tool_failed'])  # закрытая словом хозяина карточка — не сбой тула
            # вопрос хозяину с вариантами — тоже закрывается словом
            turn = asyncio.ensure_future(app.mind.say(env.sid, 'спроси меня'))
            await until(lambda: app.questions)
            await app.mind.say(env.sid, WORD + ' 2', wait=False)
            await asyncio.wait_for(turn, 5)
            self.assertFalse(app.questions)
            tool = [m for m in app.store.messages(env.sid) if m['role'] == 'tool'][-1]
            self.assertIn('Вопрос закрыт без ответа', tool['content'])

    @async_test
    async def test_interrupt_cancels_with_valid_pairs_and_starts_new_turn(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            env.auto_approve('once')
            self.listen(app)
            app.settings.set('agent', {'new_message': 'interrupt'})
            env.script([{'tool_calls': [{'name': 'run', 'arguments': {'command': 'sleep 8'}}, {'name': 'plan', 'arguments': {'items': ['x']}}]},
                        {'text': 'Отвечаю на новое.'}])
            first = asyncio.ensure_future(app.mind.say(env.sid, 'долгая задача'))
            await until(lambda: any(e.get('type') == 'tool_start' and e.get('name') == 'run' for e in self.events))
            res = await asyncio.wait_for(app.mind.say(env.sid, WORD), 5)
            old = await first
            self.assertEqual(old['error']['code'], 'stopped')
            self.assertEqual(old['error']['title'], 'Ход прерван новым сообщением')
            self.assertEqual(res['text'], 'Отвечаю на новое.')
            self.assertEqual(len(self.kinds('turn_start')), 2)
            rows = [m for m in app.store.messages(env.sid) if m['role'] != 'event']
            self.assertTrue(pairs_ok(rows))
            interrupted = [m for m in rows if m['role'] == 'tool']
            self.assertEqual(len(interrupted), 2)
            self.assertTrue(all('Прервано хозяином' in m['content'] for m in interrupted))
            word = rows[-2]
            self.assertEqual((word['role'], word['meta']['delivery']), ('user', 'interrupt'))
            self.assertIn('Ход прерван: хозяин написал новое сообщение.', [m['content'] for m in app.store.messages(env.sid) if m.get('kind') == 'stopped'])
            self.assertFalse(app.guard.halted)  # не стоп-кран

    @async_test
    async def test_queue_waits_for_turn_end_then_starts_new_turn_and_stop_keeps_it(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            env.auto_approve('once')
            self.listen(app)
            app.settings.set('agent', {'new_message': 'queue'})
            env.script([call('run', command='sleep 1'), {'text': 'Первое готово.'}, {'text': 'Теперь твоё.'}])
            first = asyncio.ensure_future(app.mind.say(env.sid, 'долгая задача'))
            await until(lambda: any(e.get('type') == 'tool_start' for e in self.events))
            self.assertIsNone(await app.mind.say(env.sid, WORD, wait=False))
            self.assertEqual(app.mind.pending(env.sid)['queue'], 1)
            self.assertFalse([m for m in app.store.messages(env.sid) if WORD in str(m.get('content'))])  # не в истории, модель не видит
            res = await first
            self.assertEqual(res['text'], 'Первое готово.')
            self.assertNotIn('команда НЕ остановлена', [m for m in app.store.messages(env.sid) if m['role'] == 'tool'][0]['content'])
            self.assertIsNone(seen(env))  # ход шёл — модель слова не видела
            await until(lambda: len(self.kinds('turn_end')) == 2)
            self.assertEqual(seen(env), 2)  # только новым ходом
            self.assertEqual(len(self.kinds('turn_start')), 2)
            word = next(m for m in app.store.messages(env.sid) if WORD in str(m.get('content')))
            self.assertEqual(word['meta']['delivery'], 'queue')
            self.assertNotIn('mid_turn', word['meta'])
            # Стоп с отложенным словом: оно в истории, новый ход не начинается
            started = asyncio.Event()

            @app.tools.tool('hold', 'Ждать', group='base')
            async def hold(ctx):
                started.set()
                await asyncio.Event().wait()
            env.script([call('hold')])
            first = asyncio.ensure_future(app.mind.say(env.sid, 'ещё'))
            await started.wait()
            await app.mind.say(env.sid, WORD + ' после стопа', wait=False)
            app.mind.stop(env.sid)
            await first
            await asyncio.sleep(0.05)
            self.assertFalse(app.mind.running(env.sid))
            self.assertTrue(any(WORD + ' после стопа' in str(m.get('content')) for m in app.store.messages(env.sid)))
            self.assertIn('новый ход не начат', app.store.messages(env.sid)[-1]['content'])

    @async_test
    async def test_redeliver_buttons_move_pending_words(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            self.listen(app)
            started = asyncio.Event()

            @app.tools.tool('hold', 'Ждать', group='base')
            async def hold(ctx):
                started.set()
                await steer.wait(steer.event(ctx.turn), asyncio.sleep(30))  # кооперативное ожидание
                return ToolResult('дождался')
            app.settings.set('agent', {'new_message': 'queue'})
            env.script([call('hold'), {'text': 'Поздно.'}, {'text': 'Ответ на прерывание.'}])
            first = asyncio.ensure_future(app.mind.say(env.sid, 'жди'))
            await started.wait()
            await app.mind.say(env.sid, WORD, wait=False)
            self.assertEqual(app.mind.pending(env.sid)['queue'], 1)
            self.assertEqual(await app.mind.redeliver(env.sid, 'insert'), 1)  # «↪ Вставить»: ждущий тул возвращается
            self.assertEqual(await app.mind.redeliver(env.sid, 'queue'), 1)
            self.assertEqual(app.mind.pending(env.sid), {**app.mind.pending(env.sid), 'insert': 0, 'queue': 1})
            self.assertFalse(steer.pending(app.mind.turns[env.sid].state))
            self.assertEqual(await app.mind.redeliver(env.sid, 'interrupt'), 1)  # «⚡ Прервать и ответить»
            await first
            await until(lambda: len(self.kinds('turn_end')) == 2)
            self.assertEqual(seen(env), 1)
            self.assertEqual([e['delivery'] for e in self.kinds('pending_moved')], ['insert', 'queue', 'interrupt'])
            word = next(m for m in app.store.messages(env.sid) if WORD in str(m.get('content')))
            self.assertEqual(word['meta']['delivery'], 'interrupt')
            self.assertEqual(await app.mind.redeliver(env.sid, 'insert'), 0)  # хода нет — нечего менять

    @async_test
    async def test_flock_neighbour_wait_is_not_cut(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            env.auto_approve('once')
            other = app.agents.get(app.agents.create('Сосед', agent_id='neighbour', awaken=False)['id'])
            provider = app.models.roles()['main'][0]['provider']
            other.models.set_role('main', [{'provider': provider, 'model': 'tools'}])
            env.script([call('run', command='sleep 8'), {'text': 'Понял.'}])
            mine = asyncio.ensure_future(app.mind.say(env.sid, 'долгая задача'))
            theirs = asyncio.ensure_future(other.mind.say(other.main_session(), 'tool:ask_owner {"question":"Какой?","options":["а","б"]}'))
            await until(lambda: other.questions and app.mind.turns.get(env.sid) and app.mind.turns[env.sid].state.get('_doing', {}).get('what') == 'выполняет тул run')
            await app.mind.say(env.sid, WORD, wait=False)
            await asyncio.wait_for(mine, 5)
            self.assertTrue(other.questions)  # вопрос соседа по стае открыт: слово было не ему
            self.assertFalse(steer.pending(other.mind.turns[other.main_session()].state))
            other.answer(next(iter(other.questions)), 'а')
            res = await asyncio.wait_for(theirs, 5)
            self.assertIn('Ответ хозяина: а', res['text'])
            await app.processes.close()


class SteerClaudeCodeTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.log = self.tmp / 'claude.log'
        os.environ['FAKE_CLAUDE_LOG'] = str(self.log)

    def tearDown(self):
        for key in ('FAKE_CLAUDE_LOG', 'FAKE_CLAUDE'):
            os.environ.pop(key, None)
        clipkg._FLAGS.clear()
        super().tearDown()

    def runs(self):
        return [json.loads(x) for x in self.log.read_text(encoding='utf-8').splitlines()] if self.log.exists() else []

    def engine(self, env):
        provider = env.app.models.add_provider(preset='claude_code', extra={'command': [sys.executable, str(FAKE_CLAUDE)]})
        env.app.models.set_role('main', [{'provider': provider['id'], 'model': 'default'}])
        return provider

    @async_test
    async def test_word_reaches_cli_loop_in_next_tool_result_with_prompt_nonce(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            env.auto_approve('once')
            self.engine(env)
            os.environ['FAKE_CLAUDE'] = json.dumps({'tools': [{'name': 'run', 'arguments': {'command': 'sleep 8'}},
                                                              {'name': 'plan', 'arguments': {'items': ['дальше']}}], 'text': 'Сделал иначе.'})
            events = []
            app.bus.listen(lambda ev: events.append(ev))
            turn = asyncio.ensure_future(app.mind.say(env.sid, 'долгая задача'))
            await until(lambda: any(e.get('type') == 'tool_start' and e.get('name') == 'run' for e in events))
            await app.mind.say(env.sid, WORD, wait=False)
            res = await asyncio.wait_for(turn, 8)
            self.assertEqual(res['text'], 'Сделал иначе.')
            runs = self.runs()
            self.assertEqual(len(runs), 1)  # весь цикл — один вызов CLI, слово дошло внутри него
            nonce = runs[0]['prompt'].split('⟦хозяин ', 1)[1][:8]
            first = runs[0]['results'][0]['text']
            self.assertIn('команда НЕ остановлена', first)
            self.assertIn(f'⟦хозяин {nonce}⟧', first)
            rows = app.store.messages(env.sid)
            word = next(i for i, m in enumerate(rows) if m['role'] == 'user' and WORD in str(m['content']))
            answer = next(i for i, m in enumerate(rows) if m['role'] == 'assistant')
            self.assertLess(word, answer)  # в истории до итогового ответа — следующему вызову не отправится снова
            self.assertTrue(rows[word]['meta']['mid_turn'])
            os.environ['FAKE_CLAUDE'] = json.dumps({'text': 'Помню.'})
            await app.mind.say(env.sid, 'дальше')
            self.assertNotIn(WORD, self.runs()[1]['prompt'])
            await app.processes.close()

    @async_test
    async def test_pending_word_stops_next_cli_tool_call(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            outside = app.dirs.work / 'cli.txt'
            release = asyncio.Event()

            async def loop_task():
                await release.wait()
            sid = env.sid
            from bender.mind.loop import Turn
            turn = Turn(sid, 'pult')
            turn.task = asyncio.ensure_future(loop_task())
            turn.state['_cli_nonce'] = 'abcd1234'
            app.mind.turns[sid] = turn
            try:
                await app.mind.say(sid, WORD, wait=False)
                self.assertTrue(steer.pending(turn.state))
                server = app.router.cli_bridge.server
                result = await server.call('write_file', {'path': str(outside), 'content': 'x'}, Binding(turn, None))
                text = '\n'.join(c['text'] for c in result['content'] if c['type'] == 'text')
                self.assertIn('не выполнен', text)
                self.assertIn('⟦хозяин abcd1234⟧', text)
                self.assertFalse(outside.exists())
                self.assertFalse(steer.pending(turn.state))
                self.assertTrue(any(WORD in str(m['content']) for m in app.store.messages(sid)))
                result = await server.call('write_file', {'path': str(outside), 'content': 'x'}, Binding(turn, None))
                self.assertFalse(result['isError'])  # следующий вызов — обычный
            finally:
                release.set()
                await turn.task
                app.mind.turns.pop(sid, None)


class SteerTelegramTest(TempDirCase):
    _up = original.TelegramTest._up
    push = original.TelegramTest.push
    sent = original.TelegramTest.sent
    until = original.TelegramTest.until

    def reactions(self, env):
        return [r['reaction'][0]['emoji'] for r in self.sent(env, 'setMessageReaction') if r.get('reaction')]

    @async_test
    async def test_modes_reactions_status_buttons_and_mode_command(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            env.auto_approve('once')
            await self._up(env, owner=True)
            app.settings.set('telegram', {'live_via': 'message'})
            tg = app.telegram
            # ↪ вставить (по умолчанию): 👀, строка в статусе хода и кнопки разового выбора
            env.script([call('run', command='sleep 8'), {'text': 'Понял, иначе.'}])
            self.push(env, 'долгая задача')
            await self.until(lambda: app.mind.turns.get(env.sid) and app.mind.turns[env.sid].state.get('_doing', {}).get('what') == 'выполняет тул run')
            self.push(env, WORD)
            await self.until(lambda: any(m.get('text') == 'Понял, иначе.' for m in self.sent(env)))
            self.assertIn('👀', self.reactions(env))
            self.assertTrue(next(m for m in app.store.messages(env.sid) if WORD in str(m.get('content')))['meta']['mid_turn'])
            await app.processes.close()
            # ⏳ после хода: ✍ и кнопка «⚡ Прервать и ответить» под статусом
            app.settings.set('agent', {'new_message': 'queue'})
            started = asyncio.Event()

            @app.tools.tool('hold', 'Ждать', group='base')
            async def hold(ctx):
                started.set()
                await asyncio.Event().wait()
            env.script([call('hold'), {'text': 'Ответ на отложенное.'}])
            self.push(env, 'ещё задача')
            await asyncio.wait_for(started.wait(), 5)
            self.push(env, WORD + ' 2')
            await self.until(lambda: '✍' in self.reactions(env))
            await self.until(lambda: any('nm:interrupt' in json.dumps(m.get('reply_markup') or {}) for m in self.sent(env, 'editMessageText') + self.sent(env)))
            status = next(m for m in self.sent(env, 'editMessageText') + self.sent(env) if 'nm:interrupt' in json.dumps(m.get('reply_markup') or {}))
            self.assertIn('После хода', status['text'])
            mid = status.get('message_id') or next(r['message_id'] for r in env.fake.state.tg_sent if r.get('disable_notification') and r['method'] == 'sendMessage')
            # кнопка «⚡ Прервать и ответить»
            self.push(env, callback_query={'id': 'cb1', 'from': {'id': 42}, 'data': 'nm:interrupt',
                                           'message': {'message_id': mid, 'chat': {'id': 42, 'type': 'private'}}})
            await self.until(lambda: any(m.get('text') == 'Ответ на отложенное.' for m in self.sent(env)))
            word = next(m for m in app.store.messages(env.sid) if WORD + ' 2' in str(m.get('content')))
            self.assertEqual(word['meta']['delivery'], 'interrupt')
            # /delivery: выбор по умолчанию командой и в /status
            self.push(env, '/mode прервать')  # прежнее имя — синоним
            await self.until(lambda: steer.default(app) == 'interrupt')
            self.push(env, '/status')
            await self.until(lambda: any('Сообщение во время работы: ⚡ Прерывать сразу' in str(m.get('text')) for m in self.sent(env)))
            self.push(env, '/delivery')
            await self.until(lambda: any('Сообщение во время работы:' in str(m.get('text')) and m.get('reply_markup') for m in self.sent(env)))
            await self.until(lambda: not tg._bg)
