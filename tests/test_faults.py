"""Порча и нехватка места не уничтожают исходные данные и не оставляют полутранзакцию."""
import asyncio
import errno
import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch
from bender.core.app import App
from bender.core.errors import HumanError
from bender.core.fsutil import atomic_write_text
from bender.core.secrets import Secrets
from bender.core.store import Store
from tests.helpers import TempDirCase,async_test


class FaultTest(TempDirCase):
    def test_corrupt_database_is_human_error_and_preserved(self):
        path=self.tmp/'data'/'bender.db';path.parent.mkdir();path.write_bytes(b'broken database')
        with self.assertRaises(HumanError):App(path.parent)
        self.assertEqual(path.read_bytes(),b'broken database')

    def test_secrets_corruption_and_failed_save_preserve_values(self):
        path=self.tmp/'secrets.json';path.write_text('{"token":')
        secrets=Secrets(path)
        with self.assertRaises(HumanError):secrets.set('new','replacement')
        self.assertEqual(path.read_text(),'{"token":')
        path.unlink();secrets=Secrets(path);secrets.set('key','old-value')
        with patch('bender.core.fsutil.os.replace',side_effect=OSError(errno.ENOSPC,'disk full')):
            with self.assertRaises(HumanError):secrets.set('key','new-value')
        self.assertEqual(secrets.get('key'),'old-value')
        self.assertEqual(Secrets(path).get('key'),'old-value')
        secrets.set('key','retry-value');self.assertEqual(Secrets(path).get('key'),'retry-value')
        self.assertEqual(list(self.tmp.glob('*.bdtmp')),[])

    def test_settings_and_empty_identity_fail_without_overwriting(self):
        app=App(self.tmp/'data')
        app.store.x("INSERT OR REPLACE INTO settings(grp,value,updated_at) VALUES('guard','{',0)");app.store.close()
        with self.assertRaises(HumanError):App(self.tmp/'data')
        db=sqlite3.connect(self.tmp/'data'/'bender.db')
        try:db.execute("DELETE FROM settings WHERE grp='guard'");db.commit()
        finally:db.close()
        path=self.tmp/'data'/'self'/'IDENTITY.md';path.write_text('')
        with self.assertRaises(HumanError):App(self.tmp/'data')
        self.assertEqual(path.read_text(),'')

    def test_failed_commit_rolls_back_and_next_transaction_works(self):
        store=Store(self.tmp/'bender.db');real=store.db
        class FailCommit:
            fail=True
            def __getattr__(self,name):return getattr(real,name)
            def execute(self,sql,*args):
                if sql=='COMMIT' and self.fail:
                    self.fail=False
                    raise sqlite3.OperationalError('database or disk is full')
                return real.execute(sql,*args)
        store.db=FailCommit()
        try:
            with self.assertRaises(HumanError):
                with store.tx() as db:db.execute("INSERT INTO meta VALUES('lost','1')")
            self.assertIsNone(store.meta_get('lost'))
            with store.tx() as db:db.execute("INSERT INTO meta VALUES('saved','1')")
            self.assertEqual(store.meta_get('saved'),1)
        finally:store.close()

    @async_test
    async def test_recovery_marks_interrupted_turn_once(self):
        app=App(self.tmp/'data');sid=app.main_session()
        first=app.store.add_message(sid,{'role':'user','content':'Прерванная просьба'},{'actor':'owner'})
        app.store.update_session(sid,meta={'active_turn':{'first':first,'channel':'pult'}})
        try:
            self.assertEqual(app.mind.recover(),1)
            self.assertEqual(app.mind.recover(),0)
            event=app.store.messages(sid)[-1]
            self.assertEqual(event['kind'],'interrupted')
            self.assertIn('прерван',event['content'].lower())
            self.assertIsNone(app.store.session(sid)['meta']['active_turn'])
        finally:await app.close()

    def test_corrupt_boot_refuses_without_restart_or_overwrite(self):
        from bender.core.watchdog import Watchdog,load_boot
        path=self.tmp/'local'/'boot.txt';path.parent.mkdir()
        for raw in (b'garbage',b'starts\tnan\n',b'safe\tbad\n',b'slots.timeout\tbad\n',b'slots.nonce\t123\n',b'\xff',b''):
            path.write_bytes(raw)
            self.assertTrue(load_boot(self.tmp).get('_error'))
            watcher=Watchdog(self.tmp,["unused"],out=lambda text:None)
            self.assertEqual(watcher.run(),78)
            self.assertEqual(path.read_bytes(),raw)

    @async_test
    async def test_limits_helper_in_tmp_survives_private_tmp_mount(self):
        import shlex,sys
        from bender.guard import sandbox
        if sandbox.detect()['kind']!='bwrap':self.skipTest('Нужен настоящий bubblewrap')
        app=App(self.tmp/'data');app.settings.set('guard',{'process_count':1024})
        helper=self.tmp/'limits.py';helper.write_bytes(sandbox.CHILD.read_bytes())
        try:
            with patch.object(sandbox,'CHILD',helper):
                with sandbox.prepare(shlex.join([sys.executable,'-c',"print('ok')"]),info=sandbox.for_app(app),work_dir=app.dirs.work,allow_net=False) as prepared:
                    proc=await asyncio.create_subprocess_exec(*prepared.argv,env={**os.environ,**prepared.env},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                    out,err=await asyncio.wait_for(proc.communicate(),5)
                    self.assertEqual(proc.returncode,0,err.decode())
                    self.assertEqual(out.strip(),b'ok')
        finally:await app.close()

    @async_test
    async def test_update_copy_failure_keeps_current_and_can_retry(self):
        from bender.core.paths import PROGRAM_DIR
        from bender.core.watchdog import load_boot
        import shutil
        app=App(self.tmp/'data')
        try:
            app.settings.set('updates',{'allow_development':True})
            record=app.versions.stage(PROGRAM_DIR,development=True)
            def broken(source,target,*args,**kwargs):
                Path(target).mkdir();(Path(target)/'partial').write_text('неполная копия')
                raise OSError(errno.ENOSPC,'disk full')
            with patch('bender.updates.manager.shutil.copytree',side_effect=broken):
                with self.assertRaises(HumanError):app.versions.install(record['id'],record['hash'])
            self.assertIsNone(app.store.meta_get('updates.in_progress'))
            self.assertFalse(app.versions.slot(record['id']).exists())
            self.assertFalse(load_boot(app.dirs.root).get('slots',{}).get('pending'))
            with patch('bender.updates.manager.save_boot',return_value=False):
                with self.assertRaises(HumanError):app.versions.install(record['id'],record['hash'])
            app.versions.install(record['id'],record['hash'])
            self.assertEqual(load_boot(app.dirs.root)['slots']['pending'],record['id'])
            self.assertIsNone(app.store.meta_get('updates.in_progress'))
        finally:await app.close()

    def test_repeated_failed_construction_closes_database(self):
        from bender.devtools.measure import children
        root=self.tmp/'data';app=App(root);app.store.close()
        (root/'self'/'IDENTITY.md').write_text('')
        descriptors=Path('/proc/self/fd')
        if not descriptors.exists():self.skipTest('Счётчик FD доступен через procfs')
        before=len(list(descriptors.iterdir()))
        for _ in range(20):
            with self.assertRaises(HumanError):App(root)
        self.assertLessEqual(len(list(descriptors.iterdir())),before+2)
