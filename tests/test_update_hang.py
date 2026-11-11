"""Обновление из пульта не висит молча: git не ждёт чужих потомков, бекап не перечитывает неизменное,
копия базы не держит общий замок, шаг обновления виден."""
import asyncio
import os
import subprocess
import threading
import time
from unittest.mock import patch

from bender.backups import manager as backups_module
from bender.core.app import App
from bender.core.store import Store
from bender.updates.git import command
from tests.helpers import TempDirCase, async_test
from tests import test_git_update as original


class UpdateHangTest(TempDirCase):
    @async_test
    async def test_command_returns_when_descendant_keeps_pipes_open(self):
        if os.name != 'posix': self.skipTest('setsid есть только в POSIX')
        # Так ведёт себя помощник, ушедший в свою сессию: git закончил, а труба вывода ещё открыта.
        marker = 'bender-test-' + str(os.getpid())
        started = time.monotonic()
        try:
            code, out = await asyncio.wait_for(command(['sh', '-c', f'setsid sh -c "sleep 60; : {marker}" & echo готово'], self.tmp, timeout=20), 15)
        finally:
            subprocess.run(['pkill', '-f', marker], check=False)
        self.assertEqual((code, out), (0, 'готово\n'))
        self.assertLess(time.monotonic() - started, 10)

    @async_test
    async def test_second_snapshot_reads_only_changed_files(self):
        app = App(self.tmp / 'data')
        try:
            app.settings.set('backups', {'path': str(self.tmp / 'copies')})
            for n in range(5): (app.dirs.work / f'файл{n}.txt').write_text('данные ' * 1000 + str(n))
            first = await app.backups.now()
            self.assertTrue(app.backups.progress['first'])
            self.assertLess(first['read'], 1.5 * first['bytes'], 'новый файл не считается дважды (проверка + копия)')
            self.assertIn('work', [item['folder'] for item in first['heavy']])
            changed = app.dirs.work / 'файл3.txt'; changed.write_text('новое содержимое')
            read = []
            original = backups_module.file_hash
            with patch.object(backups_module, 'file_hash', lambda path: read.append(str(path)) or original(path)):
                result = await app.backups.now()
            names = ' '.join(read)
            self.assertIn('файл3.txt', names)
            for n in (0, 1, 2, 4): self.assertNotIn(f'файл{n}.txt', names)
            self.assertTrue((await app.backups.verify(full=True))['ok'])
            self.assertEqual(app.backups.progress['files'], app.backups.progress['total'])
            self.assertGreater(result['files'], 5)
            self.assertFalse(app.backups.progress['first'])
            self.assertLess(result['read'], first['read'])
        finally:
            await app.close()

    def test_database_copy_does_not_hold_store_lock(self):
        store = Store(self.tmp / 'bender.db')
        try:
            store.meta_set('проверка', 1)
            done = threading.Event()
            with store._lock:  # основной поток занят базой — копия всё равно идёт
                worker = threading.Thread(target=lambda: (store.backup_to(self.tmp / 'copy.db'), done.set()), daemon=True)
                worker.start()
                self.assertTrue(done.wait(5))
        finally:
            store.close()


class UpdateProgressTest(TempDirCase):
    git = original.GitUpdateTest.git
    setup_repo = original.GitUpdateTest.setup_repo
    publish = original.GitUpdateTest.publish
    application = original.GitUpdateTest.application

    @async_test
    async def test_progress_shows_step_during_backup_and_last_result(self):
        self.setup_repo(); self.publish(); app = self.application()
        try:
            plan = await app.updater.check()
            self.assertIsNone(app.updater.progress()['step'])
            entered, release = asyncio.Event(), asyncio.Event()
            backup = app.updater.backup

            async def slow():
                entered.set(); await release.wait()
                return await backup()

            with patch.object(app.updater, 'backup', slow):
                task = asyncio.ensure_future(app.updater.install(plan))
                await asyncio.wait_for(entered.wait(), 10)
                self.assertTrue(app.updater.progress()['step']['title'].startswith('Бекап'))
                release.set()
                result = await asyncio.wait_for(task, 60)
            self.assertTrue(result['updated'])
            state = app.updater.progress()
            self.assertIsNone(state['step'])
            self.assertTrue(state['last']['ok'])
            self.assertIn('Программа обновлена', state['last']['message'])
        finally:
            await app.close()
