"""Учёт сообщений Telegram: строка на сообщение вместо переписи общего JSON, прежние JSON читаются."""
import io
import json
import time
import zipfile
from types import SimpleNamespace as NS
from unittest.mock import patch

from bender.agents.store import NamespaceStore
from bender.channels import telegram_refs as refs
from bender.channels.telegram_topics import Address
from bender.core.store import Store
from tests.helpers import TempDirCase, async_test


class TelegramRefsTest(TempDirCase):
    def host(self):
        store = Store(self.tmp / 'bender.db')
        self.addCleanup(store.close)
        return NS(store=store, agent_id='main')

    def agent(self, host, agent_id='fox'):
        return NS(store=NamespaceStore(host.store, agent_id), agent_id=agent_id, host=host)

    def raw_meta(self, store, key):
        row = store.one('SELECT value FROM meta WHERE key = ?', (key,))
        return row['value'] if row else None

    def test_keys_match_previous_format_including_topic_address(self):
        for chat, mid in ((42, 5), (Address(42, 300), 5), (-1001234567890, 77), (Address(-1001234567890, 9), 1)):
            self.assertEqual(refs.key(chat, mid), str(chat) + ':' + str(mid))
        self.assertEqual(refs.key(Address(42, 300), 5), '42:5')
        self.assertIsNone(refs.key(42, None))
        self.assertIsNone(refs.key(None, 5))
        host = self.host()
        refs.remember(host, host, Address(42, 300), 5, {'session': 's1', 'text': 'Тема', 'direction': 'out', 'thread': 300})
        self.assertEqual(refs.ref(host, 42, 5)['thread'], 300)  # прочитать можно и по голому номеру чата
        self.assertEqual(refs.route(host, Address(42, 301), 5), {'agent': 'main', 'session': 's1'})
        self.assertEqual(host.store.one('SELECT key FROM telegram_routes')['key'], '42:5')

    def test_round_trip_update_keeps_unknown_fields_and_agents_are_separate(self):
        host = self.host()
        fox = self.agent(host)
        refs.put_ref(fox, 42, 10, {'session': 's1', 'text': 'Первое', 'direction': 'in', 'future': {'x': 1}})
        refs.put_routes(host, 42, [10, 11], 'fox', 's1')
        self.assertEqual(refs.ref(fox, 42, 10)['text'], 'Первое')
        self.assertIsNone(refs.ref(host, 42, 10))  # у главного своя таблица
        self.assertEqual(refs.route(fox, 42, 11), {'agent': 'fox', 'session': 's1'})  # маршруты всегда у хозяина
        changed = refs.update_ref(fox, 42, 10, text='Второе', edit_date=21)
        self.assertEqual(changed, refs.ref(fox, 42, 10))
        self.assertEqual(changed['future'], {'x': 1})
        self.assertEqual((changed['text'], changed['edit_date'], changed['direction']), ('Второе', 21, 'in'))
        self.assertIsNone(refs.update_ref(fox, 42, 999, text='нет'))
        self.assertEqual(refs.drop_route(host, 42, 11), 1)
        self.assertIsNone(refs.route(host, 42, 11))
        self.assertEqual(refs.count(fox), 1)
        tables = {r['name'] for r in host.store.q("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'telegram_routes', 'telegram_refs', 'agent_fox__telegram_refs'} <= tables)
        self.assertNotIn('agent_fox__telegram_routes', tables)

    def test_remember_list_of_ids_is_one_transaction_and_keeps_meta_untouched(self):
        from bender.channels.telegram import Telegram
        host = self.host()
        fox = self.agent(host)
        tg = Telegram(host)
        tg.remember(fox, 42, 1, 's0', 'Разогрев', 'out')
        statements = []
        host.store.db.set_trace_callback(statements.append)
        try:
            tg.remember(fox, Address(42, 7), [101, 102, 103], 's1', 'Длинный ответ кусками', 'out', tainted=True)
        finally:
            host.store.db.set_trace_callback(None)
        self.assertEqual(len([s for s in statements if s.startswith('BEGIN')]), 1)
        self.assertFalse([s for s in statements if 'meta' in s and s.lstrip().upper().startswith(('INSERT', 'UPDATE'))])
        for mid in (101, 102, 103):
            data = refs.ref(fox, 42, mid)
            self.assertEqual((data['session'], data['thread'], data['tainted'], data['direction']), ('s1', 7, True, 'out'))
            self.assertEqual(refs.route(host, 42, mid), {'agent': 'fox', 'session': 's1'})
        tg.remember(fox, 42, ['не номер', None, True], 's1', 'Пусто', 'out')
        tg.remember(fox, 42, None, 's1', 'Пусто', 'out')
        self.assertEqual(refs.count(fox), 4)
        self.assertIsNone(self.raw_meta(fox.store, 'telegram:refs'))
        self.assertIsNone(self.raw_meta(host.store, 'telegram:message_routes'))
        tg.remember(host, 42, 200, 's2', 'Вход', 'in', source_text='Вход', command=True)
        self.assertTrue(tg.require_ref(host, 42, 200, 's2')['command'])
        from bender.core.errors import HumanError
        with self.assertRaises(HumanError):
            tg.require_ref(host, 42, 200, 'другая')
        with self.assertRaises(HumanError):
            tg.require_ref(fox, 42, 200, 's2')

    def test_previous_json_is_imported_once_never_rewritten_and_new_old_slot_writes_follow(self):
        host = self.host()
        old_refs = {'42:1': {'session': 's1', 'text': 'Прежнее', 'direction': 'in', 'source_text': 'Прежнее'},
                    '42:2': {'session': 's1', 'text': 'Ответ', 'direction': 'out', 'unknown': True}}
        host.store.meta_set('telegram:refs', old_refs)
        host.store.meta_set('telegram:message_routes', {'42:1': {'agent': 'main', 'session': 's1'}, 'битый': 'x'})
        fox_store = NamespaceStore(host.store, 'fox')
        fox_store.meta_set('telegram:refs', {'42:3': {'session': 'f1', 'text': 'Лисье', 'direction': 'out'}})
        before = {k: self.raw_meta(host.store, k) for k in ('telegram:refs', 'telegram:message_routes')}
        self.assertEqual(refs.ref(host, 42, 2)['unknown'], True)
        self.assertEqual(refs.route(host, 42, 1), {'agent': 'main', 'session': 's1'})
        fox = NS(store=fox_store, agent_id='fox', host=host)
        self.assertEqual(refs.ref(fox, 42, 3)['text'], 'Лисье')
        self.assertIsNone(refs.ref(fox, 42, 1))
        refs.update_ref(host, 42, 1, text='Исправлено')
        refs.remember(host, host, 42, 5, {'session': 's1', 'text': 'Новое', 'direction': 'out'})
        # Прежние JSON не меняются: прежний слот после отката читает их как были.
        self.assertEqual({k: self.raw_meta(host.store, k) for k in before}, before)
        self.assertEqual(host.store.meta_get('telegram:refs'), old_refs)
        # Новый процесс: JSON не разбирается повторно, если прежний слот его не трогал.
        again = Store(self.tmp / 'bender.db')
        self.addCleanup(again.close)
        seen = []
        original = again.meta_get
        def meta_get(key, default=None):
            seen.append(key)
            return original(key, default)
        with patch.object(again, 'meta_get', meta_get):
            self.assertEqual(refs.ref(NS(store=again), 42, 1)['text'], 'Исправлено')
            self.assertEqual(refs.route(NS(store=again), 42, 5), {'agent': 'main', 'session': 's1'})
        self.assertNotIn('telegram:refs', seen)
        self.assertNotIn('telegram:message_routes', seen)
        # Прежний слот после отката дописал своё: при следующем запуске это переносится, строки таблицы главнее.
        older = dict(old_refs, **{'42:1': {'session': 's1', 'text': 'Версия прежнего слота', 'direction': 'in'},
                                  '42:9': {'session': 's1', 'text': 'Написано при откате', 'direction': 'in'}})
        host.store.meta_set('telegram:refs', older)
        third = Store(self.tmp / 'bender.db')
        self.addCleanup(third.close)
        self.assertEqual(refs.ref(NS(store=third), 42, 9)['text'], 'Написано при откате')
        self.assertEqual(refs.ref(NS(store=third), 42, 1)['text'], 'Исправлено')
        self.assertEqual(host.store.meta_get('telegram:refs'), older)
        # Испорченный прежний JSON не ломает учёт.
        host.store.x("UPDATE meta SET value = '{битый' WHERE key = 'telegram:message_routes'")
        fourth = NS(store=Store(self.tmp / 'bender.db'))
        self.addCleanup(fourth.store.close)
        self.assertIsNone(refs.route(fourth, 42, 777))
        refs.put_routes(fourth, 42, 777, 'main', 's1')
        self.assertEqual(refs.route(fourth, 42, 777), {'agent': 'main', 'session': 's1'})

    def test_remember_is_cheap_with_thousands_of_long_refs(self):
        from bender.channels.telegram import Telegram
        host = self.host()
        text = 'Длинный ответ агента. ' * 140  # ~3000 знаков
        host.store.meta_set('telegram:refs', {'42:' + str(i): {'session': 's1', 'text': text[:3000], 'direction': 'out', 'thread': None,
                                                              'tainted': False, 'foreign': False} for i in range(3000)})
        host.store.meta_set('telegram:message_routes', {'42:' + str(i): {'agent': 'main', 'session': 's1'} for i in range(3000)})
        before = {k: self.raw_meta(host.store, k) for k in ('telegram:refs', 'telegram:message_routes')}
        tg = Telegram(host)
        tg.remember(host, 42, 5000, 's1', 'Разогрев: перенос прежнего JSON', 'out')
        self.assertEqual(refs.count(host), 3001)
        spent = []
        for n in range(30):
            started = time.perf_counter()
            tg.remember(host, Address(42, 3), 6000 + n, 's1', text[:3000], 'in', source_text=text[:3000], tainted=True)
            spent.append(time.perf_counter() - started)
        spent.sort()
        # Прежде каждое сообщение разбирало и переписывало JSON на мегабайты (десятки—сотни мс).
        self.assertLess(spent[len(spent) // 2], 0.005, spent)
        self.assertLess(sum(spent), 0.5, spent)
        self.assertEqual({k: self.raw_meta(host.store, k) for k in before}, before)
        self.assertEqual(refs.ref(host, 42, 6029)['thread'], 3)

    def test_prune_keeps_newest_without_deleting_on_every_insert(self):
        host = self.host()
        fox = self.agent(host)
        clock = iter(range(1, 10000))
        with patch.dict(refs.KEEP, {refs.REFS: 5, refs.ROUTES: 7}), patch.object(refs, 'PRUNE_EVERY', 3), \
                patch.object(refs, 'time', NS(time=lambda: float(next(clock)))):
            statements = []
            host.store.db.set_trace_callback(statements.append)
            try:
                for mid in range(1, 21):
                    refs.remember(host, fox, 42, mid, {'session': 's1', 'text': str(mid), 'direction': 'out'})
            finally:
                host.store.db.set_trace_callback(None)
        deletes = [s for s in statements if s.lstrip().upper().startswith('DELETE')]
        self.assertLess(len(deletes), 20)  # редкая обрезка, не на каждую запись
        kept = [int(r['key'].split(':')[1]) for r in fox.store.q('SELECT key FROM telegram_refs ORDER BY created')]
        self.assertIn(20, kept)
        self.assertLessEqual(len(kept), 5 + 3)
        self.assertEqual(kept, list(range(21 - len(kept), 21)))  # уходят старейшие
        routes = [r['message'] for r in host.store.q('SELECT message FROM telegram_routes ORDER BY created')]
        self.assertLessEqual(len(routes), 7 + 3)
        self.assertEqual(routes, list(range(21 - len(routes), 21)))

    @async_test
    async def test_agent_export_skips_message_bookkeeping_and_old_archive_imports(self):
        from tests.test_mind import MindEnv
        async with MindEnv(self.tmp) as env:
            host = env.app
            host.agents.create('Лис', agent_id='fox', awaken=False)
            fox = host.agents.get('fox')
            host.telegram.remember(fox, 42, 77, fox.main_session(), 'Сообщение лиса', 'out')
            host.telegram.remember(host, 42, 78, env.sid, 'Сообщение главного', 'out')
            data = json.loads(zipfile.ZipFile(io.BytesIO(host.agents.export('fox'))).read('agent.json'))
            self.assertNotIn('telegram_refs', data['tables'])
            main = json.loads(zipfile.ZipFile(io.BytesIO(host.agents.export('main'))).read('agent.json'))
            self.assertFalse({'telegram_refs', 'telegram_routes'} & set(main['tables']))
            # Архив другой версии с таблицей учёта: не переносится; архив без неё — как прежде.
            for n, tables in enumerate((dict(data['tables'], telegram_refs=[{'key': '42:77', 'session': 'x', 'direction': 'out', 'data': '{}', 'created': 1.0}]),
                                        data['tables'])):
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, 'w') as archive:
                    archive.writestr('agent.json', json.dumps({**data, 'tables': tables}, ensure_ascii=False))
                copy = host.agents.get(host.agents.import_archive(buffer.getvalue(), name='Копия ' + str(n))['id'])
                self.assertIsNone(refs.ref(copy, 42, 77))
                self.assertEqual(refs.route(copy, 42, 77)['agent'], 'fox')
