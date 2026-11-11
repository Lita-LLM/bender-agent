"""Инкремент, порча, согласованная SQLite и совместимость описей."""
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
from bender.core.app import App
from bender.core.errors import HumanError
from tests.helpers import TempDirCase, async_test


class BackupTest(TempDirCase):
    @async_test
    async def test_increment_restore_and_corruption(self):
        app = App(self.tmp / 'data')
        try:
            app.settings.set('backups', {'path': str(self.tmp/'disk1'), 'second_path':str(self.tmp/'disk2')})
            (app.dirs.work/'hello.txt').write_text('память')
            executable = app.dirs.work/'task.py'
            executable.write_text('#!/usr/bin/env python3\nprint(1)\n')
            executable.chmod(0o755)
            app.secrets.set('fixture', 'test-only-token')
            first = await app.backups.now()
            second = await app.backups.now()
            self.assertLess(second['copied'], first['copied'])
            self.assertTrue((await app.backups.verify(full=True))['ok'])
            root = app.backups.roots()[0]
            manifest = app.backups.manifest(root, first['id'])
            self.assertNotIn('secrets.json', manifest['files'])
            target = self.tmp/'restored'
            self.assertIn('work/hello.txt', app.backups.preview(first['id'], target)['changed'])
            await app.backups.restore(first['id'], target)
            self.assertEqual((target/'work/hello.txt').read_text(), 'память')
            self.assertEqual((target/'work/task.py').read_bytes(),executable.read_bytes())
            if os.name=='posix':self.assertEqual((target/'work/task.py').stat().st_mode & 0o777, 0o700)
            with closing(sqlite3.connect(target/'bender.db')) as db:
                self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                self.assertGreater(db.execute('SELECT count(*) FROM journal').fetchone()[0], 0)
            reopened = App(target)
            self.assertTrue(reopened.journal.verify()['ok'])
            await reopened.close()
            raw = manifest['files']['work/hello.txt']['hash']
            app.backups.block(root, raw).write_text('порча')
            self.assertFalse((await app.backups.verify(full=True))['ok'])
            with self.assertRaises(HumanError):
                await app.backups.restore(first['id'], self.tmp/'bad')
            app.backups.block(root, raw).unlink()
            with self.assertRaises(HumanError):
                await app.backups.restore(first['id'], self.tmp/'missing')
        finally:
            await app.close()

    @async_test
    async def test_old_format_secrets_and_future_schema(self):
        app=App(self.tmp/'data')
        try:
            app.settings.set('backups', {'path':str(self.tmp/'disk'), 'include_secrets':True})
            app.secrets.set('fixture','test-only')
            saved=await app.backups.now()
            root=app.backups.roots()[0]
            path=root/'snapshots'/(saved['id']+'.json')
            value=json.loads(path.read_text())
            self.assertIn('secrets.json',value['files'])
            value['format']=0
            value.pop('schemas')
            path.write_text(json.dumps(value))
            target=self.tmp/'legacy'
            await app.backups.restore(saved['id'],target)
            if os.name=='posix':self.assertEqual((target/'secrets.json').stat().st_mode & 0o777,0o600)
            value['schemas']={'schema:base':999}
            path.write_text(json.dumps(value))
            with self.assertRaises(HumanError): app.backups.preview(saved['id'], self.tmp/'future')
            with self.assertRaises(HumanError): app.backups.preview(saved['id'], app.dirs.root)
            with self.assertRaises(HumanError): app.settings.set('backups', {'path':'/tmp'},by='agent')
            for broken in ('[]', '{', 'недопустимый JSON'):
                path.write_text(broken)
                with self.assertRaises(HumanError): app.backups.manifest(root, saved['id'])
        finally: await app.close()

    @async_test
    async def test_retention_schedule_and_disabled(self):
        from unittest.mock import patch
        app=App(self.tmp/'data')
        try:
            app.settings.set('backups', {'path':str(self.tmp/'disk'), 'auto_hours':24, 'keep_last':1, 'daily':0, 'weekly':0, 'monthly':0})
            for _ in range(3):
                await app.backups.now()
            self.assertEqual(len(app.backups.snapshots(app.backups.roots()[0])),1)
            self.assertIsNotNone(app.backups.due())
            with patch.object(app.heart,'owner_idle',return_value=True):
                await app.backups.tick()
            state=app.store.meta_get('backups.state',{})
            self.assertGreater(state['last_full'],0)
            app.settings.set('backups',{'enabled':False})
            self.assertIsNone(app.backups.due())
            with self.assertRaises(HumanError): await app.backups.now()
        finally: await app.close()

    @async_test
    async def test_ambiguous_snapshot_is_not_published(self):
        app=App(self.tmp/'data')
        try:
            app.settings.set('backups', {'path':str(self.tmp/'disk')})
            for first,second in (('same.txt','SAME.txt'),('café.txt','café.txt')):
                a,b=app.dirs.work/first,app.dirs.work/second
                a.write_text('один');b.write_text('другой')
                if a.samefile(b):
                    a.unlink()
                    continue
                with self.assertRaises(HumanError): await app.backups.now()
                self.assertEqual(app.backups.snapshots(app.backups.roots()[0]), [])
                a.unlink();b.unlink()
        finally: await app.close()


class BackupLightTest(TempDirCase):
    """Бекап перед обновлением не копирует сотни мегабайт заново."""

    def _calls(self, app, agent_store):
        for store in (app.store, agent_store):
            store.add_call(session_id=None, role='main', provider='p', model='m', usage={}, cost=0, seconds=1, notice=None,
                           request={'messages': [{'role': 'user', 'content': 'длинная история ' * 500}]})

    @async_test
    async def test_rebuildable_skipped_and_db_slim(self):
        app = App(self.tmp / 'data')
        try:
            app.settings.set('backups', {'path': str(self.tmp / 'disk')})
            other = app.agents.get(app.agents.create('Сосед', agent_id='neighbor', awaken=False)['id'])
            self._calls(app, other.store)
            proj = app.dirs.work / 'proj'
            (proj / 'node_modules' / 'lib').mkdir(parents=True)
            (proj / 'node_modules' / 'lib' / 'index.js').write_text('x' * 1000)
            (proj / '.venv' / 'lib').mkdir(parents=True)
            (proj / '.venv' / 'pyvenv.cfg').write_text('home = /usr')
            (proj / '.venv' / 'lib' / 'big.so').write_bytes(b'0' * 1000)
            (proj / 'main.py').write_text('print(1)')
            voice = app.dirs.root / 'local' / 'voice'
            voice.mkdir(parents=True, exist_ok=True)
            (voice / 'model.onnx').write_bytes(b'1' * 1000)
            (other.dirs.work / 'neighbor-bot' / 'node_modules').mkdir(parents=True)
            (other.dirs.work / 'neighbor-bot' / 'node_modules' / 'a.js').write_text('a')
            (other.dirs.work / 'neighbor-bot' / 'session.json').write_text('{}')
            result = await app.backups.now()
            manifest = app.backups.manifest(app.backups.roots()[0], result['id'])
            files = set(manifest['files'])
            self.assertIn('work/proj/main.py', files)
            self.assertIn('agents/neighbor/work/neighbor-bot/session.json', files)
            self.assertFalse([f for f in files if 'node_modules' in f or '.venv' in f or f.startswith('local/voice')], files)
            self.assertIn('work/proj/node_modules', manifest['skipped'])
            self.assertIn('local/voice', manifest['skipped'])
            self.assertEqual(result['skipped'], 5)  # и копии базы перед миграциями
            target = self.tmp / 'restored'
            await app.backups.restore(result['id'], target)
            with closing(sqlite3.connect(target / 'bender.db')) as db:
                for table in ('calls', 'agent_neighbor__calls'):
                    self.assertEqual(db.execute(f'SELECT count(*), count(request) FROM "{table}"').fetchone(), (1, 0), table)
            # в живой базе снимки остаются для диагностики
            self.assertEqual(app.store.one('SELECT count(request) AS n FROM calls')['n'], 1)
            # хозяин может вернуть полный бекап
            app.settings.set('backups', {'skip_rebuildable': False})
            full = await app.backups.now()
            files = set(app.backups.manifest(app.backups.roots()[0], full['id'])['files'])
            self.assertIn('work/proj/node_modules/lib/index.js', files)
            self.assertIn('local/voice/model.onnx', files)
        finally:
            await app.close()

    @async_test
    async def test_migration_copies_stay_out_and_few(self):
        """Копии базы перед миграциями: не в бекапе, на диске — только последние."""
        from bender.core.store import MIGRATION_COPIES
        app = App(self.tmp / 'data')
        try:
            app.settings.set('backups', {'path': str(self.tmp / 'disk'), 'skip_rebuildable': False})
            folder = app.dirs.root / 'local' / 'migration-backups'
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.iterdir():
                old.unlink()
            for i in range(5):
                (folder / f'0000{i}.db').write_bytes(b'x' * 1000)
            result = await app.backups.now()
            manifest = app.backups.manifest(app.backups.roots()[0], result['id'])
            self.assertFalse([f for f in manifest['files'] if 'migration-backups' in f])
            self.assertIn('local/migration-backups', manifest['skipped'])
            self.assertEqual(app.housekeeping()['migration_copies'], 5 - MIGRATION_COPIES)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ['00003.db', '00004.db'][-MIGRATION_COPIES:])
            app.store._migration_backup = False
            app.store.migrate('probe-copies', 1, ['CREATE TABLE IF NOT EXISTS probe_copies (x)'])
            self.assertEqual(len(list(folder.glob('*.db'))), MIGRATION_COPIES)
            self.assertNotIn('00003.db', [p.name for p in folder.iterdir()])
        finally:
            await app.close()

    @async_test
    async def test_update_fallback_keeps_few_snapshots(self):
        from bender.updates.git import UPDATE_SNAPSHOTS
        app = App(self.tmp / 'data')
        try:
            root = app.backups.destination(str(self.tmp / 'upd'))
            for _ in range(UPDATE_SNAPSHOTS + 3):
                app.backups._snapshot([root], None, UPDATE_SNAPSHOTS)
            self.assertEqual(len(app.backups.snapshots(root)), UPDATE_SNAPSHOTS)
        finally:
            await app.close()

    @async_test
    async def test_housekeeping_prunes_every_agent(self):
        import time
        app = App(self.tmp / 'data')
        try:
            other = app.agents.get(app.agents.create('Сосед', agent_id='neighbor', awaken=False)['id'])
            self._calls(app, other.store)
            app.store.db.execute('UPDATE agent_neighbor__calls SET created_at = ?', (time.time() - 30 * 86400,))
            app.store.db.execute('UPDATE calls SET created_at = ?', (time.time() - 30 * 86400,))
            old = other.dirs.checkpoints / '2020-01-01'
            old.mkdir(parents=True)
            (old / 'x').write_text('старый снимок')
            done = app.housekeeping()
            self.assertEqual(done['call_snapshots'], 2)
            self.assertFalse(old.exists())
            self.assertEqual(app.store.one('SELECT count(request) AS n FROM agent_neighbor__calls')['n'], 0)
        finally:
            await app.close()


class BackupHealingTest(TempDirCase):
    """Сбои носителя и файлов не должны отменять снимок, если есть из чего его собрать."""

    def app(self, second=True):
        app = App(self.tmp / 'data')
        app.settings.set('backups', {'path': str(self.tmp / 'disk1'), **({'second_path': str(self.tmp / 'disk2')} if second else {})})
        (app.dirs.work / 'hello.txt').write_text('память')
        return app

    @async_test
    async def test_bad_block_on_one_disk_does_not_cancel_snapshot(self):
        app = self.app()
        try:
            first = await app.backups.now()
            first_root, second_root = app.backups.roots()
            digest = app.backups.manifest(first_root, first['id'])['files']['work/hello.txt']['hash']
            block = app.backups.block(second_root, digest)
            block.unlink()
            block.mkdir()  # блок «не читается»: вместо файла каталог
            changed = await app.backups.now()
            self.assertEqual(changed['destinations'], 1)
            self.assertTrue(any('Носитель пропущен' in w for w in changed['warnings']))
            self.assertIn(changed['id'], app.backups.snapshots(first_root))
            self.assertNotIn(changed['id'], app.backups.snapshots(second_root))
        finally:
            await app.close()

    @async_test
    async def test_all_disks_bad_gives_actionable_error(self):
        app = self.app(second=False)
        try:
            first = await app.backups.now()
            root = app.backups.roots()[0]
            digest = app.backups.manifest(root, first['id'])['files']['work/hello.txt']['hash']
            block = app.backups.block(root, digest)
            block.unlink()
            block.mkdir()
            (app.dirs.work / 'hello.txt').write_text('память')  # тот же текст: блок нужен прежний, а он не читается
            with self.assertRaises(HumanError) as caught:
                await app.backups.now()
            self.assertIn('Починить бекапы', caught.exception.hint)
        finally:
            await app.close()

    @async_test
    async def test_vanished_file_is_a_warning_not_a_failure(self):
        app = self.app(second=False)
        try:
            ghost = app.dirs.work / 'ghost.txt'
            ghost.write_text('временный')
            from unittest import mock
            from bender.backups import manager
            real = manager.file_hash

            def flaky(path):
                if Path(path).name == 'ghost.txt':  # файл исчез или не читается между обходом и чтением
                    raise HumanError('backup_read', 'Не удалось прочитать файл бекапа', '')
                return real(path)
            with mock.patch.object(manager, 'file_hash', flaky):
                result = await app.backups.now()
            self.assertTrue(any('ghost.txt' in w for w in result['warnings']), result['warnings'])
        finally:
            await app.close()

    @async_test
    async def test_repair_takes_good_blocks_from_other_copy(self):
        app = self.app()
        try:
            first = await app.backups.now()
            first_root, second_root = app.backups.roots()
            digest = app.backups.manifest(first_root, first['id'])['files']['work/hello.txt']['hash']
            app.backups.block(first_root, digest).write_text('порча')
            app.backups.block(second_root, digest).write_text('порча')  # нигде целой копии нет
            (second_root / 'snapshots' / (first['id'] + '.json')).unlink()  # и опись на втором носителе потеряна
            other = app.backups.block(first_root, next(h for h in {o['hash'] for o in app.backups.manifest(first_root, first['id'])['files'].values()} if h != digest))
            other.write_text('порча')  # а этот блок цел на втором носителе
            result = await app.backups.repair(snapshot=False)
            self.assertEqual(result['manifests'], 1)
            self.assertGreaterEqual(result['fixed'], 1)
            self.assertEqual(result['lost'], 1)  # hello.txt без целых копий — честно «потеряно»
            self.assertIn(first['id'], result['damaged'])
            self.assertFalse(app.backups.block(first_root, digest).exists())  # битый блок убран, чтобы новый снимок записал файл заново
            fresh = await app.backups.now()
            self.assertGreater(fresh['copied'], 0)
        finally:
            await app.close()

    @async_test
    async def test_snapshot_failure_with_unreadable_block_heals_itself_once(self):
        app = self.app()
        try:
            await app.backups.now()
            calls = []
            real = app.backups._snapshot

            def flaky(roots, *a, **k):
                calls.append(1)
                if len(calls) == 1:
                    raise HumanError('backup_read', 'Не удалось прочитать файл бекапа', 'Проверь носитель.')
                return real(roots, *a, **k)
            app.backups._snapshot = flaky
            result = await app.backups.now()
            self.assertEqual(len(calls), 2)
            self.assertIn('healed', result)
            self.assertTrue(app.store.meta_get('backups.state')['repair']['auto'])
        finally:
            await app.close()
