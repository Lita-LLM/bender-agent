"""Копия базы перед миграцией (охота 26.11: «Бендер завис и перезапустился: цикл стоял в backup_to» дважды подряд).

Раньше: копия снималась под общим замком базы, у каждого агента стаи своя (флаг копировался по значению), и всё это
в потоке цикла, пока сторож не убивал процесс через 180 с молчания. Теперь: одна копия на файл, вне замка, большая — в
фоне со снимком, зафиксированным до миграции."""
import sqlite3
import threading
import time

from bender.agents.store import NamespaceStore
from bender.core import store as store_module
from bender.core.store import Store
from tests.helpers import TempDirCase

PROBE = ['CREATE TABLE IF NOT EXISTS probe_{n} (x)']


class MigrationCopyTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / 'bender.db')
        self.folder = self.tmp / 'local' / 'migration-backups'
        self.addCleanup(self.store.close)

    def copies(self):
        return sorted(self.folder.glob('*.db'))

    def clear(self):
        for path in self.folder.iterdir():
            path.unlink()
        self.store._migration_backup = False

    def lock_is_free(self) -> bool:
        """Берётся ли замок базы из другого потока прямо сейчас (и тут же отпускается им же)."""
        result = []

        def take():
            got = self.store._lock.acquire(False)
            if got:
                self.store._lock.release()
            result.append(got)

        thread = threading.Thread(target=take)
        thread.start()
        thread.join(5)
        return bool(result and result[0])

    def probe(self, store, n):
        store.migrate(f'probe-{n}', 1, [s.format(n=n) for s in PROBE])

    def test_agents_of_the_flock_share_one_copy(self):
        self.clear()
        first, second, third = (NamespaceStore(self.store, name) for name in ('first', 'second', 'third'))
        self.assertEqual(len(self.copies()), 1, 'у каждого агента была своя полная копия той же базы')
        self.assertTrue(first._migration_backup and second._migration_backup and third._migration_backup)
        self.probe(second, 1)
        self.probe(self.store, 2)
        self.assertEqual(len(self.copies()), 1)

    def test_copy_is_taken_outside_the_store_lock(self):
        self.clear()
        real, seen = self.store.backup_to, []

        def spying(path):
            seen.append(self.lock_is_free())
            real(path)

        self.store.backup_to = spying
        self.probe(self.store, 1)
        self.assertEqual(seen, [True], 'пока шла копия, общий замок базы был занят: цикл ждал бы её целиком')
        self.assertEqual(len(self.copies()), 1)

    def test_two_threads_migrating_at_once_make_one_copy_and_both_migrations_land(self):
        self.clear()
        real, started = self.store.backup_to, []

        def slow(path):
            started.append(time.monotonic())
            time.sleep(0.3)
            real(path)

        self.store.backup_to = slow
        threads = [threading.Thread(target=self.probe, args=(self.store, n)) for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(started), 1)
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual((self.store.schema_version('probe-1'), self.store.schema_version('probe-2')), (1, 1))
        # копия снята до обеих миграций: в ней их таблиц ещё нет
        copy = sqlite3.connect(str(self.copies()[0]))
        try:
            tables = {r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            copy.close()
        self.assertFalse({'probe_1', 'probe_2'} & tables, tables)

    def test_failed_copy_is_retried_by_the_next_migration(self):
        self.clear()
        real = self.store.backup_to

        def broken(path):
            raise sqlite3.OperationalError('disk I/O error')

        self.store.backup_to = broken
        with self.assertRaises(sqlite3.OperationalError):
            self.probe(self.store, 1)
        self.assertFalse(self.store._migration_backup)
        self.store.backup_to = real
        self.probe(self.store, 1)
        self.assertEqual(len(self.copies()), 1)
        self.assertTrue(self.store._migration_backup)

    def fill(self, rows=400):
        blob = b'x' * 20000
        self.store.migrate('payload', 1, ['CREATE TABLE payload (id INTEGER PRIMARY KEY, b BLOB)'])
        with self.store.tx() as db:
            db.executemany('INSERT INTO payload(b) VALUES(?)', [(blob,)] * rows)

    def test_big_database_is_copied_in_background_from_a_snapshot_taken_before_the_migration(self):
        self.fill()
        self.clear()
        saved = store_module.COPY_INLINE_BYTES, store_module.COPY_PAGES, store_module.COPY_BREATH
        store_module.COPY_INLINE_BYTES, store_module.COPY_PAGES, store_module.COPY_BREATH = 0, 32, 0.01
        self.addCleanup(lambda: [setattr(store_module, n, v) for n, v in zip(('COPY_INLINE_BYTES', 'COPY_PAGES', 'COPY_BREATH'), saved)])
        begin = time.monotonic()
        self.probe(self.store, 1)
        migrated_in = time.monotonic() - begin
        self.assertIsNotNone(self.store._copy['thread'])
        self.assertTrue(self.store._copy['thread'].is_alive(), 'копия должна идти фоном, миграция её не ждёт')
        # работа идёт, пока копия снимается: записи не мешают и в копию не попадают
        self.store.x('INSERT INTO payload(b) VALUES(?)', (b'after-snapshot',))
        self.assertEqual(self.store.schema_version('probe-1'), 1)
        self.assertTrue(self.store.wait_copy(60))
        self.assertLess(migrated_in, 1.0)
        self.assertEqual([p.name for p in self.folder.glob('*.part')], [])
        self.assertEqual(len(self.copies()), 1)
        copy = sqlite3.connect(str(self.copies()[0]))
        try:
            self.assertEqual(copy.execute('SELECT count(*) FROM payload').fetchone()[0], 400)
            self.assertIsNone(copy.execute("SELECT 1 FROM sqlite_master WHERE name='probe_1'").fetchone())
            self.assertEqual(copy.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            copy.close()
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM payload')['n'], 401)

    def test_closing_cuts_off_a_copy_that_would_not_finish_and_leaves_no_scraps(self):
        self.fill(800)
        self.clear()
        saved = store_module.COPY_INLINE_BYTES, store_module.COPY_PAGES, store_module.COPY_BREATH
        store_module.COPY_INLINE_BYTES, store_module.COPY_PAGES, store_module.COPY_BREATH = 0, 4, 0.2
        self.addCleanup(lambda: [setattr(store_module, n, v) for n, v in zip(('COPY_INLINE_BYTES', 'COPY_PAGES', 'COPY_BREATH'), saved)])
        self.probe(self.store, 1)
        thread = self.store._copy['thread']
        self.assertTrue(thread.is_alive())
        begin = time.monotonic()
        self.store.finish_copy(0.3)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - begin, 5)
        self.assertEqual(list(self.folder.iterdir()), [], 'недописанная копия не должна оставаться')

    def test_leftover_unfinished_copies_are_swept_but_a_fresh_one_is_not(self):
        self.clear()
        old, fresh = self.folder / 'old.db.part', self.folder / 'fresh.db.part'
        old.write_bytes(b'x' * 100)
        fresh.write_bytes(b'x' * 100)
        past = time.time() - store_module.PART_STALE - 60
        import os
        os.utime(old, (past, past))
        self.store.prune_migration_copies()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_checkpoint_does_not_wait_for_a_long_reader_under_the_lock(self):
        """Копия базы читает минутами; сброс журнала ждал её до busy_timeout (10 с) под общим замком — молчал весь Бендер."""
        self.fill(50)
        reader = sqlite3.connect(str(self.tmp / 'bender.db'), isolation_level=None)
        reader.execute('BEGIN')
        reader.execute('SELECT count(*) FROM payload').fetchone()
        self.store.x('INSERT INTO payload(b) VALUES(?)', (b'new',))  # у журнала есть что сбрасывать, а читатель держит старый снимок
        try:
            begin = time.monotonic()
            self.store.checkpoint()
            self.assertLess(time.monotonic() - begin, 2.0)
            self.assertEqual(self.store.one('PRAGMA busy_timeout')[0], 10000)  # ожидание базы вернулось прежним
        finally:
            reader.close()
        self.store.checkpoint()  # читателя нет — журнал сбрасывается

    def test_old_call_snapshots_are_pruned_in_pieces_and_the_lock_is_free_between_them(self):
        saved = store_module.PRUNE_CHUNK
        store_module.PRUNE_CHUNK = 10
        self.addCleanup(lambda: setattr(store_module, 'PRUNE_CHUNK', saved))
        now = time.time()
        rows = [(f'old{i}', now - 30 * 86400, b'r' * 100) for i in range(37)] + [(f'new{i}', now, b'r' * 100) for i in range(5)]
        with self.store.tx() as db:
            db.executemany('INSERT INTO calls(id, created_at, request) VALUES(?,?,?)', rows)
        updates, free = [], []
        self.store.db.set_trace_callback(lambda sql: updates.append(sql) if sql.startswith('UPDATE "calls"') else None)
        real_sleep = time.sleep

        def spying_sleep(seconds):
            free.append(self.lock_is_free())
            real_sleep(0)

        store_module.time = type('T', (), {'time': staticmethod(time.time), 'sleep': staticmethod(spying_sleep)})
        try:
            pruned = self.store.prune_call_requests(7)
        finally:
            store_module.time = time
            self.store.db.set_trace_callback(None)
        self.assertEqual(pruned, 37)
        self.assertEqual(len(updates), 4, updates)  # 10 + 10 + 10 + 7
        self.assertTrue(free and all(free), 'между кусками уборки замок базы занят')
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM calls WHERE request IS NOT NULL')['n'], 5)


if __name__ == '__main__':
    import unittest
    unittest.main()
