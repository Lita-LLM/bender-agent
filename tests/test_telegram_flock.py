"""Стая в одном чате бота: параллельные ходы, одно место черновика, куски подряд со знаком, маршруты всего
отправленного и ответ цитатой — агенту сообщения, с пометкой и ответом цитатой на слово хозяина."""
import asyncio
import itertools
from unittest.mock import patch

from bender.agents.badge import PALETTE, emoji
from bender.channels import telegram_refs
from bender.channels.telegram_live import Live
from bender.channels.telegram_routes import agent_name
from bender.core.dates import content
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call
from tests import test_telegram as original


def owner(mid, text, reply=None, thread=None, **extra):
    message = {'message_id': mid, 'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}, 'text': text, **extra}
    if reply is not None: message['reply_to_message'] = {'message_id': reply, 'from': {'id': 777, 'is_bot': True}}
    if thread is not None: message.update(message_thread_id=thread, is_topic_message=True)
    return {'message': message}


def words(head, n):
    return head + ' ' + ' '.join('слово%d' % i for i in range(n))


class FlockTelegramTest(TempDirCase):
    _up = original.TelegramTest._up
    until = original.TelegramTest.until
    sent = original.TelegramTest.sent

    async def flock(self, env, mode='draft', names=('Луна',), topics=False):
        if topics: env.fake.state.tg_topics = True
        await self._up(env, owner=True)
        app = env.app
        app.settings.set('telegram', {'agent_label': True, 'live_via': mode})
        agents = [app.agents.get(app.agents.create(name, agent_id='a' + str(n), awaken=False)['id']) for n, name in enumerate(names)]
        return app, app.telegram, agents

    def requests(self, env):
        return [r for r in env.fake.state.requests if r.get('api') == 'openai']

    def answers(self, env):
        """Сообщения ответов (без статусов хода, «✓» и отклонённых попыток)."""
        return [m for m in self.sent(env) if not m.get('disable_notification') and not m.get('rejected')]

    def statuses(self, env):
        return [m for m in self.sent(env) if m.get('disable_notification') and m.get('text') != '✓']

    def user_text(self, ctx, word):
        rows = [m for m in ctx.store.messages(ctx.main_session()) if m['role'] == 'user' and word in str(m['content'])]
        return rows[-1] if rows else None

    def owner_of(self, ctxs, text):
        found = [c for c in ctxs if text.startswith(emoji(c) + ':') or text.startswith(emoji(c) + ' (')]
        return found[0] if len(found) == 1 else None

    @async_test
    async def test_parallel_turns_share_one_draft_slot_status_routes_and_contiguous_badged_chunks(self):
        # Три агента стаи работают одновременно в одной личке (темы выключены или общий поток при темах) — в обоих видах живого ответа.
        for mode, topics in itertools.product(('draft', 'message'), (False, True)):
            async with MindEnv(self.tmp / (mode + str(topics))) as env:
                app, tg, (luna, owl) = await self.flock(env, mode, ('Луна', 'Сова'), topics=topics)
                self.assertEqual(tg.topics.enabled(42), topics)
                everyone = (app, luna, owl)
                env.script([{'text': words('Главный.', 900), 'delay': .002}, {'text': words('Луна.', 900), 'delay': .002},
                            {'text': words('Сова.', 900), 'delay': .002}])
                with patch('bender.channels.telegram_live.DRAFT_INTERVAL', .05), patch('bender.channels.telegram_live.FALLBACK_INTERVAL', .05):
                    for n, ctx in enumerate(everyone):
                        tg.choose(42, ctx)
                        await tg._handle(owner(1001 + n, 'Работай'))
                        await self.until(lambda: len(self.requests(env)) == n + 1)
                    tg.choose(42, app)
                    await self.until(lambda: len([m for m in self.answers(env) if '(3/3)' in m['text']]) == 3, timeout=30)
                    await self.until(lambda: not tg._bg, timeout=10)
                drafts = self.sent(env, 'sendMessageDraft')
                if mode == 'draft':
                    self.assertTrue(drafts)
                    self.assertEqual(len({d['draft_id'] for d in drafts}), 1, 'черновик ведёт один ход: без «🤖/🦊» попеременно')
                    holder = self.owner_of(everyone, drafts[0]['text'])
                    self.assertTrue(holder and all(d['text'].startswith(emoji(holder)) for d in drafts))
                    self.assertEqual(len(self.statuses(env)), 2, 'соседи — статус-сообщением')
                else:
                    self.assertFalse(drafts)
                    self.assertEqual(len(self.statuses(env)), 3)
                for status in self.statuses(env):
                    who = self.owner_of(everyone, status['text'])
                    self.assertIsNotNone(who, status['text'])
                    self.assertEqual(telegram_refs.route(app, 42, status['message_id'])['agent'], who.agent_id, 'ответ на статус цитатой — его агенту')
                self.assertEqual({m['message_id'] for m in self.sent(env, 'deleteMessage')} >= {m['message_id'] for m in self.statuses(env)}, True)
                chunks = self.answers(env)
                self.assertEqual(len(chunks), 9)
                order = [self.owner_of(everyone, m['text']) for m in chunks]
                self.assertTrue(all(order), [m['text'][:12] for m in chunks])
                self.assertEqual(len([k for k, _ in itertools.groupby(o.agent_id for o in order)]), 3, 'куски одного ответа идут подряд')
                for who, group in itertools.groupby(zip(order, chunks), key=lambda pair: pair[0].agent_id):
                    group = [m for _, m in group]
                    ctx = next(c for c in everyone if c.agent_id == who)
                    self.assertTrue(group[0]['text'].startswith(emoji(ctx) + ': '))
                    self.assertEqual([m['text'][:len(emoji(ctx)) + 6] for m in group[1:]], [emoji(ctx) + ' (2/3)', emoji(ctx) + ' (3/3)'])
                    for m in group:
                        route = telegram_refs.route(app, 42, m['message_id'])
                        self.assertEqual((route['agent'], route['session']), (ctx.agent_id, ctx.main_session()), 'каждый кусок — за своим агентом')
                self.assertIs(tg.selected(42)[0], app)

    @async_test
    async def test_concurrent_long_answers_do_not_interleave(self):
        async with MindEnv(self.tmp) as env:
            app, tg, (luna, owl) = await self.flock(env, 'message', ('Луна', 'Сова'))
            everyone = (app, luna, owl)
            texts = {ctx.agent_id: ('Абзац ' + agent_name(ctx) + '. ' + 'слово ' * 600 + '\n\n') * 3 for ctx in everyone}
            await asyncio.gather(*(tg._answer(42, ctx.main_session(), {'text': texts[ctx.agent_id]}, agent_ctx=ctx) for ctx in everyone))
            chunks = self.answers(env)
            order = [self.owner_of(everyone, m['text']) for m in chunks]
            self.assertTrue(all(order))
            self.assertGreaterEqual(len(chunks), 6)
            self.assertEqual(len([k for k, _ in itertools.groupby(o.agent_id for o in order)]), 3, [o.agent_id for o in order])
            for ctx, m in zip(order, chunks):
                self.assertEqual(telegram_refs.route(app, 42, m['message_id'])['agent'], ctx.agent_id)
                self.assertEqual(telegram_refs.ref(ctx, 42, m['message_id'])['direction'], 'out')

    @async_test
    async def test_quote_to_busy_neighbour_status_reaches_it_with_note_and_threaded_answer(self):
        async with MindEnv(self.tmp) as env:
            app, tg, (luna,) = await self.flock(env, 'message')
            lsid = luna.main_session()
            env.script([{'text': words('Луна работает.', 80), 'delay': .02}, {'text': 'Да, это я — Луна.'}])
            with patch('bender.channels.telegram_live.FALLBACK_INTERVAL', .03):
                tg.choose(42, luna)
                await tg._handle(owner(1001, 'Луна, поработай'))
                await self.until(lambda: self.statuses(env))
                status = self.statuses(env)[0]['message_id']
                tg.choose(42, app)  # хозяин вернулся к главному
                await tg._handle(owner(1002, 'Это ты?', reply=status))
                await self.until(lambda: any(r.get('message_id') == 1002 for r in self.sent(env, 'setMessageReaction')))
                self.assertTrue(luna.mind.running(lsid), 'слово встало в идущий ход соседа')
                await self.until(lambda: any('Да, это я' in m['text'] for m in self.answers(env)), timeout=15)
                await self.until(lambda: not tg._bg)
            answer = next(m for m in self.answers(env) if 'Да, это я' in m['text'])
            self.assertTrue(answer['text'].startswith(emoji(luna) + ': '))
            self.assertEqual(answer['reply_parameters']['message_id'], 1002, 'ответ соседа — цитатой на слово хозяина')
            said = self.user_text(luna, 'Это ты?')
            text = content(said)
            self.assertIn('[Telegram · служебно: хозяин ответил цитатой на твоё сообщение #' + str(status), text)
            self.assertIn('пока у него выбран «' + agent_name(app) + '»', text)
            self.assertIn('статус хода', text)
            self.assertFalse(said['meta'].get('untrusted'), 'цитата своего статуса и служебная строка ход не заражают')
            self.assertEqual(said['meta']['telegram_reply_routed']['selected_agent'], 'main')
            self.assertEqual(said['meta']['telegram_reply_to'], 1002)
            self.assertIsNone(self.user_text(app, 'Это ты?'))
            self.assertIs(tg.selected(42)[0], app, 'выбор хозяина не меняется')
            # следующее сообщение без цитаты — снова выбранному главному, ответ без цитаты
            env.script([{'text': 'Главный на связи.'}])
            await tg._handle(owner(1003, 'Привет'))
            await self.until(lambda: any('Главный на связи' in m['text'] for m in self.answers(env)))
            plain = next(m for m in self.answers(env) if 'Главный на связи' in m['text'])
            self.assertNotIn('reply_parameters', plain)
            self.assertNotIn('служебно', content(self.user_text(app, 'Привет')))

    @async_test
    async def test_quote_to_command_output_goes_to_its_agent(self):
        """Ответ цитатой на вывод команды (/status) — агенту, о котором была команда, а не выбранному сейчас."""
        async with MindEnv(self.tmp) as env:
            app, tg, (luna,) = await self.flock(env, 'message')
            tg.choose(42, luna)
            before = len(self.sent(env))
            await tg._handle(owner(1001, '/status'))
            await self.until(lambda: len(self.sent(env)) > before)
            reply = self.sent(env)[before]['message_id']
            self.assertEqual(telegram_refs.route(app, 42, reply)['agent'], luna.agent_id)
            env.script([{'text': 'Луна про статус.'}])
            tg.choose(42, app)
            await tg._handle(owner(1002, 'Что это значит?', reply=reply))
            await self.until(lambda: any('Луна про статус' in m['text'] for m in self.answers(env)), timeout=15)
            await self.until(lambda: not tg._bg)
            self.assertIsNotNone(self.user_text(luna, 'Что это значит?'))
            self.assertIsNone(self.user_text(app, 'Что это значит?'))
            # /agent меняет выбор: ответ на его вывод — новому выбранному
            before = len(self.sent(env))
            await tg._handle(owner(1003, '/agent ' + agent_name(luna)))
            await self.until(lambda: len(self.sent(env)) > before)
            self.assertEqual(tg.selected(42)[0].agent_id, luna.agent_id)
            self.assertEqual(telegram_refs.route(app, 42, self.sent(env)[before]['message_id'])['agent'], luna.agent_id)

    @async_test
    async def test_quote_to_neighbour_chunk_and_approval_card(self):
        async with MindEnv(self.tmp) as env:
            app, tg, (luna,) = await self.flock(env, 'message')
            lsid = luna.main_session()
            await tg._answer(42, lsid, {'text': ('Длинный ответ Луны. ' + 'слово ' * 700 + '\n\n') * 2}, agent_ctx=luna)
            first = self.answers(env)[0]['message_id']
            env.script([{'text': 'Это про первую часть.'}])
            await tg._handle(owner(2001, 'А начало?', reply=first))
            await self.until(lambda: any('первую часть' in m['text'] for m in self.answers(env)))
            answer = next(m for m in self.answers(env) if 'первую часть' in m['text'])
            self.assertEqual(answer['reply_parameters']['message_id'], 2001)
            said = self.user_text(luna, 'А начало?')
            self.assertIn('Длинный ответ Луны', content(said))
            self.assertIn('служебно: хозяин ответил цитатой на твоё сообщение #' + str(first), content(said))
            self.assertFalse(said['meta'].get('untrusted'))
            await self.until(lambda: any(r.get('message_id') == 2001 for r in self.sent(env, 'setMessageReaction')), timeout=3)  # 👀 и у свободного адресата
            # Карточка Предохранителя соседа, ход читал чужое: маршрут — к Луне, цитата карточки остаётся в обёртке.
            long = 'rm -rf logs ' + 'x' * 4000  # длиннее карточки: целиком — файлом «Действие целиком»
            card = {'id': 'card1', 'agent_id': luna.agent_id, 'session': lsid, 'tool': 'run', 'capability': 'shell.run',
                    'summary': 'удалить логи', 'reason': 'проверка', 'target': long, 'tainted': True, 'full': long}
            app.bus.publish({'type': 'approval', 'agent_id': '*', 'card': card})
            await self.until(lambda: any('card1' in str(m.get('reply_markup')) for m in self.sent(env)))
            shown = [m for m in self.sent(env) if 'card1' in str(m.get('reply_markup'))][-1]
            self.assertTrue(shown['text'].startswith(emoji(luna) + ': '))
            ref = telegram_refs.ref(luna, 42, shown['message_id'])
            self.assertEqual(telegram_refs.route(app, 42, shown['message_id'])['agent'], luna.agent_id)
            self.assertTrue(ref['tainted'] and ref['foreign'])
            documents = self.sent(env, 'sendDocument')  # «Действие целиком» — файлом перед карточкой, тоже за Луной
            self.assertEqual(len(documents), 1)
            self.assertEqual(telegram_refs.route(app, 42, documents[0]['message_id'])['agent'], luna.agent_id)
            env.script([{'text': 'Потому что место кончается.'}])
            await tg._handle(owner(2002, 'Почему?', reply=shown['message_id']))
            await self.until(lambda: self.user_text(luna, 'Почему?'))
            said = self.user_text(luna, 'Почему?')
            self.assertEqual(said['meta']['actor'], 'owner')
            self.assertTrue(said['meta'].get('untrusted'), 'карточка заражённого хода цитируется в обёртке')
            self.assertIn('служебно: хозяин ответил цитатой на твоё сообщение #' + str(shown['message_id']), content(said))
            self.assertIsNone(self.user_text(app, 'Почему?'))
            await self.until(lambda: any('место кончается' in m['text'] for m in self.answers(env)))
            self.assertEqual(next(m for m in self.answers(env) if 'место кончается' in m['text'])['reply_parameters']['message_id'], 2002)
            await self.until(lambda: not tg._bg)

    @async_test
    async def test_unavailable_quote_owner_falls_back_with_notice(self):
        async with MindEnv(self.tmp) as env:
            app, tg, (luna,) = await self.flock(env, 'message')
            lsid = luna.main_session()
            tg.remember(luna, 42, 3001, lsid, '💓 Луна: проверила почту', 'out')
            await app.agents.update(luna.agent_id, archived=True)
            env.script([{'text': 'Отвечаю за Луну.'}])
            await tg._handle(owner(3002, 'Что там?', reply=3001))
            await self.until(lambda: self.user_text(app, 'Что там?'))
            self.assertTrue(any('«Луна» в архиве — ответ передан «Главный»' in m['text'] for m in self.sent(env)), [m['text'] for m in self.sent(env)])
            said = self.user_text(app, 'Что там?')
            self.assertIn('проверила почту', content(said))
            self.assertFalse(said['meta'].get('untrusted'), 'известное сообщение соседа не становится чужим из-за архива')
            self.assertNotIn('служебно', content(said))
            await self.until(lambda: not tg._bg)
            await app.agents.update(luna.agent_id, archived=False)
            # Стая выключена — то же, с причиной.
            app.settings.set('flock', {'enabled': False})
            env.script([{'text': 'Стая спит.'}])
            await tg._handle(owner(3003, 'А сейчас?', reply=3001))
            await self.until(lambda: self.user_text(app, 'А сейчас?'))
            self.assertTrue(any('Стая выключена, «Луна» не отвечает — ответ передан «Главный»' in m['text'] for m in self.sent(env)))
            app.settings.set('flock', {'enabled': True})
            await self.until(lambda: not tg._bg)
            # Закрытый разговор соседа — в его главный, с уведомлением.
            chat_sid = luna.store.new_session('chat', 'Отдельный')
            tg.remember(luna, 42, 3101, chat_sid, 'Луна в отдельном', 'out')
            luna.store.update_session(chat_sid, meta={'archived': True})
            env.script([{'text': 'Из главной.'}])
            await tg._handle(owner(3102, 'Ответ в закрытый', reply=3101))
            await self.until(lambda: self.user_text(luna, 'Ответ в закрытый'))
            self.assertTrue(any('«Луна»: тот разговор закрыт — ответ передан в главный разговор «Луна»' in m['text'] for m in self.sent(env)))
            self.assertIn('закрытого разговора', content(self.user_text(luna, 'Ответ в закрытый')))
            await self.until(lambda: not tg._bg)
            # Временный помощник Луны: ответ — родителю, в разговор задачи, без новой «Главной» у помощника.
            app.agents.create('Кодер · abc123', agent_id='t_abc123', awaken=False, profile='custom', temporary=True)
            app.store.x('UPDATE flock_agents SET data=? WHERE id=?', ('{"profile":"custom","parent":"%s","task":"T1"}' % luna.agent_id, 't_abc123'))
            helper = app.agents.get('t_abc123')
            work = helper.store.new_session('subagent', 'Кодер: задача', meta={'task': 'T1', 'parent': lsid})
            tg.remember(helper, 42, 3201, work, '❓ Какой формат?', 'out')
            before = {r['id'] for r in helper.store.q('SELECT id FROM sessions')}
            env.script([{'text': 'Передам помощнику.'}])
            await tg._handle(owner(3202, 'JSON', reply=3201))
            await self.until(lambda: self.user_text(luna, 'JSON'))
            note = content(self.user_text(luna, 'JSON'))
            self.assertIn('сообщение твоего временного помощника «Кодер · abc123» (задача T1) #3201', note)
            self.assertEqual({r['id'] for r in helper.store.q('SELECT id FROM sessions')}, before, 'у помощника не появилась «Главная»')
            await self.until(lambda: not tg._bg)
            app.store.x('UPDATE flock_agents SET archived=1 WHERE id=?', ('t_abc123',))
            app.agents.loaded.pop('t_abc123', None)
            env.script([{'text': 'Помощник уже закончил.'}])
            await tg._handle(owner(3203, 'Ещё?', reply=3201))
            await self.until(lambda: self.user_text(luna, 'Ещё?'))
            self.assertTrue(any('Временный помощник «Кодер · abc123» уже завершён — ответ передан «Луна»' in m['text'] for m in self.sent(env)))
            await self.until(lambda: not tg._bg)
            self.assertIs(tg.selected(42)[0], app)

    @async_test
    async def test_stop_as_reply_and_list_of_working_neighbours(self):
        async with MindEnv(self.tmp) as env:
            app, tg, (luna,) = await self.flock(env, 'message')
            lsid = luna.main_session()
            env.script([{'text': words('Луна долго думает.', 400), 'delay': .02}])
            with patch('bender.channels.telegram_live.FALLBACK_INTERVAL', .03):
                tg.choose(42, luna)
                await tg._handle(owner(4001, 'Думай долго'))
                await self.until(lambda: self.statuses(env))
                status = self.statuses(env)[0]['message_id']
                tg.choose(42, app)
                await tg._handle(owner(4002, '/stop'))
                await self.until(lambda: any('Работают:' in m['text'] for m in self.sent(env)))
                listing = next(m['text'] for m in self.sent(env) if 'Работают:' in m['text'])
                self.assertIn(emoji(luna) + ' Луна', listing)
                self.assertIn('/stop цитатой', listing)
                self.assertTrue(luna.mind.running(lsid), 'простой /stop у главного соседа не трогает')
                await tg._handle(owner(4003, '/stop', reply=status))
                await self.until(lambda: not luna.mind.running(lsid))
                stopped = next(m['text'] for m in self.sent(env) if 'Остановил' in m['text'])
                self.assertTrue(stopped.startswith(emoji(luna) + ': ⏹ Остановил «Луна»'), stopped)
                await self.until(lambda: not tg._bg)

    @async_test
    async def test_agent_quotes_fragment_of_owner_message_and_retries_without_it(self):
        async with MindEnv(self.tmp) as env:
            app, tg, (luna,) = await self.flock(env, 'message')
            env.script([{'text': '↩#5001 «купи хлеба» Куплю.'}])
            await tg._handle(owner(5001, 'Зайди в магазин, купи хлеба и молока'))
            await self.until(lambda: any('Куплю' in m['text'] for m in self.answers(env)))
            answer = next(m for m in self.answers(env) if 'Куплю' in m['text'])
            self.assertEqual(answer['reply_parameters'], {'message_id': 5001, 'allow_sending_without_reply': True, 'quote': 'купи хлеба'})
            self.assertTrue(answer['text'].endswith('Куплю.'))
            await self.until(lambda: not tg._bg)
            # Не дословный кусок — ответ цитатой на всё сообщение, без фрагмента.
            env.script([{'text': '↩#5002 «купи сыра» Нет такого.'}])
            await tg._handle(owner(5002, 'И масла'))
            await self.until(lambda: any('Нет такого' in m['text'] for m in self.answers(env)))
            answer = next(m for m in self.answers(env) if 'Нет такого' in m['text'])
            self.assertEqual(answer['reply_parameters'], {'message_id': 5002, 'allow_sending_without_reply': True})
            self.assertTrue(answer['text'].endswith('«купи сыра» Нет такого.'), 'не кусок сообщения — «…» остаётся в тексте')
            await self.until(lambda: not tg._bg)
            # Telegram не принял фрагмент — повтор без него; send_message с quote.
            env.fake.state.tg_bad_quote = True
            env.script([call('send_message', text='Принято.', reply_to=5003, quote='в 10'), {'text': '↩#5003 «в 10» Записал.'}])
            await tg._handle(owner(5003, 'Встреча в 10 у входа'))
            await self.until(lambda: any('Записал' in m['text'] for m in self.answers(env)))
            await self.until(lambda: not tg._bg)
            rejected = [m for m in self.sent(env) if m.get('rejected')]
            self.assertEqual([m['reply_parameters']['quote'] for m in rejected], ['в 10', 'в 10'])
            for word in ('Принято', 'Записал'):
                answer = next(m for m in self.answers(env) if word in m['text'])
                self.assertEqual(answer['reply_parameters'], {'message_id': 5003, 'allow_sending_without_reply': True})
            self.assertTrue(any('фрагмент' in e['summary'] for e in app.journal.recent(100)))

    @async_test
    async def test_end_draft_spares_neighbour_draft_and_wakes_it(self):
        async with MindEnv(self.tmp) as env:
            app, tg, (luna,) = await self.flock(env, 'draft')
            with patch('bender.channels.telegram_live.DRAFT_INTERVAL', .01):
                a = Live(tg, 42, app.main_session(), 111, agent_ctx=app)
                a.feed({'type': 'delta', 'kind': 'text', 'text': 'A'})
                await self.until(lambda: any(m['draft_id'] == 111 for m in self.sent(env, 'sendMessageDraft')))
                await a.finish()  # A отработал, место черновика свободно
                b = Live(tg, 42, luna.main_session(), 222, agent_ctx=luna)
                tg._live[('luna', 'x')] = b
                b.feed({'type': 'delta', 'kind': 'text', 'text': 'B'})
                await self.until(lambda: any(m['draft_id'] == 222 for m in self.sent(env, 'sendMessageDraft')))
                await tg._end_draft(a)
                self.assertFalse([m for m in self.sent(env) if m.get('text') == '✓'], 'черновик соседа новее — «✓» его бы закрыл')
                # Свой черновик последний: «✓» закрывает его, соседский черновик отправляется снова сразу.
                c = Live(tg, 42, app.main_session(), 333, agent_ctx=app)
                c.drafted_at = b.drafted_at + 1
                await c.finish()
                before = len([m for m in self.sent(env, 'sendMessageDraft') if m['draft_id'] == 222])
                await tg._end_draft(c)
                self.assertTrue([m for m in self.sent(env) if m.get('text') == '✓'])
                await self.until(lambda: len([m for m in self.sent(env, 'sendMessageDraft') if m['draft_id'] == 222]) > before, timeout=2)
                await b.finish();tg._live.clear()
            self.assertEqual(tg._draft_holder, {}, 'место черновика освобождается в конце хода')

    @async_test
    async def test_topics_cards_go_to_asker_topic_and_quote_from_closed_topic(self):
        async with MindEnv(self.tmp) as env:
            app, tg, (luna,) = await self.flock(env, 'message', topics=True)
            lsid = luna.main_session()
            address = await tg.topics.create(luna, 42, lsid)
            card = {'id': 'card2', 'agent_id': luna.agent_id, 'session': lsid, 'tool': 'run', 'summary': 'перезапустить службу', 'reason': 'проверка', 'target': 'systemctl restart x'}
            app.bus.publish({'type': 'approval', 'agent_id': '*', 'card': card})
            await self.until(lambda: any('card2' in str(m.get('reply_markup')) for m in self.sent(env)))
            shown = [m for m in self.sent(env) if 'card2' in str(m.get('reply_markup'))][-1]
            self.assertEqual(shown.get('message_thread_id'), address.thread, 'карточка — в тему спросившего агента')
            question = asyncio.ensure_future(luna.ask_owner('Какой цвет?', ['Синий', 'Красный'], session_id=lsid, timeout=30))
            await self.until(lambda: any('Какой цвет' in m['text'] for m in self.sent(env)))
            self.assertEqual(next(m for m in self.sent(env) if 'Какой цвет' in m['text']).get('message_thread_id'), address.thread)
            question.cancel();await asyncio.gather(question, return_exceptions=True)
            # Тема главного закрыта: обычное сообщение там отклоняется, а ответ цитатой Луне всё равно доходит.
            sid = app.store.new_session('chat', 'Старая тема')
            closed = await tg.topics.create(app, 42, sid)
            app.store.update_session(sid, meta={'archived': True})
            tg.remember(luna, closed, 6001, lsid, 'Луна написала в общую', 'out')
            env.script([{'text': 'Луна услышала.'}])
            await tg._handle(owner(6002, 'Луна?', reply=6001, thread=closed.thread))
            await self.until(lambda: self.user_text(luna, 'Луна?'))
            self.assertIn('служебно', content(self.user_text(luna, 'Луна?')))
            await self.until(lambda: not tg._bg)
            await tg._handle(owner(6003, 'Просто так', thread=closed.thread))
            self.assertTrue(any('Разговор темы закрыт' in m['text'] for m in self.sent(env)))

    @async_test
    async def test_voice_caption_text_live_head_and_stable_badges(self):
        async with MindEnv(self.tmp) as env:
            app, tg, (luna,) = await self.flock(env, 'message')
            async def voice(*args, **kwargs): return {'audio': b'OggS-test-audio'}
            luna.settings.set('voice', {'enabled': True, 'telegram_reply': True})
            with patch.object(luna.voice, 'speak', voice):
                await tg._answer(42, luna.main_session(), {'text': 'Голосом.'}, agent_ctx=luna)
            self.assertEqual(self.sent(env, 'sendVoice')[-1]['caption'], emoji(luna), 'чей голос — видно по подписи')
            # Живой ответ «только текст» — знак агента остаётся.
            app.settings.set('telegram', {'live': 'text'})
            live = Live(tg, 42, luna.main_session(), 9, agent_ctx=luna)
            live.closed = True;await asyncio.gather(live.task, return_exceptions=True)
            live.text = 'Пишу'
            self.assertTrue(live.render().startswith(emoji(luna)))
            # Знаки: 🤖 бережётся для главного, знак агента в архиве новому соседу не достаётся.
            fresh = app.agents.get(app.agents.create('Ёж', agent_id='hedgehog', awaken=False)['id'])
            self.assertNotEqual(emoji(fresh), PALETTE[0])
            old = emoji(luna)
            await app.agents.update(luna.agent_id, archived=True)
            newer = app.agents.get(app.agents.create('Кот', agent_id='cat', awaken=False)['id'])
            newer.selfdir.set_identity({'emoji': old})
            self.assertNotEqual(emoji(newer), old, 'старые сообщения агента в архиве не выглядят словами нового')
            await app.agents.update(luna.agent_id, archived=False)
            self.assertEqual(emoji(luna), old, 'знак не переназначается')
            self.assertEqual(emoji(app), PALETTE[0])
