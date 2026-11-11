"""Живой кэш ядра и отдельные CLI не теряют роли, провайдеров и другие поля."""
import json
from pathlib import Path
import subprocess
import sys
import sqlite3
import threading
from unittest.mock import patch

from bender.core.secrets import Secrets
from bender.core.settings import Settings
from bender.core.store import Store
from bender.router.config import ModelsConfig
from tests.helpers import TempDirCase
from tests.helpers import async_test
from tests.test_mind import MindEnv


class SettingsConcurrencyTest(TempDirCase):
    @async_test
    async def test_live_app_reacts_to_external_settings_on_its_event_loop(self):
        import asyncio
        async with MindEnv(self.tmp) as env:
            app=env.app;loop=asyncio.get_running_loop();app.bus.bind_loop(loop)
            manager=app.mcp;called=asyncio.Event()
            def invalidate():
                self.assertIs(asyncio.get_running_loop(),loop);called.set()
            store=Store(app.dirs.db)
            try:Settings(store).set('mcp',{'enabled':False})
            finally:store.close()
            with patch.object(manager,'invalidate',side_effect=invalidate):
                values=await asyncio.to_thread(app.settings.group,'mcp')
                self.assertFalse(values['enabled']);await asyncio.wait_for(called.wait(),2)

    def test_slow_reader_does_not_put_old_value_back_after_writer(self):
        settings,cli=self.pair();settings.replace('voice',{'enabled':False})
        self.assertFalse(settings.get('voice','enabled'))
        cli.set('voice',{'telegram_reply':True})
        captured=threading.Event();release=threading.Event();written=threading.Event();errors=[]
        original=settings.store.one
        def read(sql,params=()):
            row=original(sql,params)
            if sql.startswith('SELECT value, updated_at') and params==('voice',) and threading.current_thread().name=='settings-reader':
                captured.set();release.wait(3)
            return row
        settings.on_change=lambda group,changes:settings.group(group)
        def reader():
            try:settings.get('voice','enabled')
            except Exception as error:errors.append(error)
        def writer():
            try:settings.set('voice',{'enabled':True})
            except Exception as error:errors.append(error)
            finally:written.set()
        left=threading.Thread(target=reader,name='settings-reader');right=threading.Thread(target=writer)
        with patch.object(settings.store,'one',side_effect=read):
            try:
                left.start();self.assertTrue(captured.wait(3));right.start()
                written.wait(.05);release.set();left.join(3);right.join(3)
                self.assertFalse(left.is_alive() or right.is_alive());self.assertFalse(errors,errors)
            finally:
                release.set();left.join(3)
                if right.ident is not None:right.join(3)
        self.assertTrue(settings.get('voice','enabled'))

    def test_external_change_notifies_once_without_background_poll(self):
        core,cli=self.pair();seen=[]
        core.on_change=lambda group,changes:seen.append((group,changes,core.group(group)))
        self.assertFalse(core.get('voice','enabled'))
        cli.set('voice',{'enabled':True})
        self.assertTrue(core.get('voice','enabled'))
        self.assertEqual(len(seen),1);self.assertTrue(seen[0][2]['enabled'])
        for _ in range(5):core.get('voice','enabled')
        cli.store.meta_set('обычное-событие',1)
        core.get('voice','enabled');self.assertEqual(len(seen),1)

    def test_settings_callbacks_wait_for_commit_and_do_not_survive_rollback(self):
        store=Store(self.tmp/'b.db');self.addCleanup(store.close)
        settings=Settings(store);seen=[]
        settings.get('voice','enabled')
        def changed(group,changes):
            db=sqlite3.connect(str(self.tmp/'b.db'))
            try:
                seen.append(json.loads(db.execute('SELECT value FROM settings WHERE grp=?',(group,)).fetchone()[0]))
            finally:db.close()
        settings.on_change=changed
        with self.assertRaises(RuntimeError):
            with store.tx():
                settings.set('voice',{'enabled':True})
                self.assertFalse(seen)
                raise RuntimeError('откат проверки')
        self.assertFalse(settings.get('voice','enabled'));self.assertFalse(seen)
        with store.tx():
            settings.set('voice',{'enabled':True});self.assertFalse(seen)
        self.assertTrue(settings.get('voice','enabled'));self.assertEqual(seen,[{'enabled':True}])

    def test_two_settings_objects_on_one_connection_see_each_other(self):
        store=Store(self.tmp/'b.db');self.addCleanup(store.close)
        first,second=Settings(store),Settings(store)
        self.assertFalse(first.get('voice','enabled'))
        second.set('voice',{'enabled':True})
        self.assertTrue(first.get('voice','enabled'))

    def pair(self):
        left=Store(self.tmp/'b.db');right=Store(self.tmp/'b.db')
        self.addCleanup(left.close);self.addCleanup(right.close)
        return Settings(left),Settings(right)

    def test_roles_written_by_cli_survive_other_role_change_in_core(self):
        core,cli=self.pair();secret=Secrets(self.tmp/'secrets.json')
        model=ModelsConfig(core,secret);p=model.add_provider(name='Локальный',base_url='http://127.0.0.1:9999/v1',extra={'is_local':True})
        entry={'provider':p['id'],'model':'проверка'}
        model.set_role('main',[entry]);core.group('roles')
        other=ModelsConfig(cli,secret)
        other.set_role('ears',[entry]);other.set_role('voice',[entry])
        cli.set('voice',{'enabled':True})
        model.set_role('fast',[entry])
        self.assertEqual(core.group('roles')['ears'],cli.group('roles')['ears'])
        self.assertEqual(core.group('roles')['voice'],cli.group('roles')['voice'])
        self.assertTrue(core.get('voice','enabled'))

    def test_provider_added_by_cli_survives_update_in_core(self):
        core,cli=self.pair();secret=Secrets(self.tmp/'secrets.json')
        model=ModelsConfig(core,secret);old=model.add_provider(name='Первый',base_url='http://127.0.0.1:9999/v1')
        model.providers()
        added=ModelsConfig(cli,secret).add_provider(preset='local_voice')
        model.update_provider(old['id'],{'name':'Изменённый'})
        providers=Settings(cli.store).group('providers')
        self.assertIn(added['id'],providers)
        self.assertEqual(providers[old['id']]['name'],'Изменённый')

    def test_external_process_changes_are_visible_on_next_access(self):
        core,_=self.pair();self.assertFalse(core.get('voice','enabled'))
        core.group('appearance')
        code="from pathlib import Path; import sys; from bender.core.store import Store; from bender.core.settings import Settings; s=Store(Path(sys.argv[1])); v=Settings(s); v.set('voice',{'enabled':True}); v.set('appearance',{'large_font':True}); s.close()"
        result=subprocess.run([sys.executable,'-c',code,str(self.tmp/'b.db')],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(core.get('voice','enabled'))
        self.assertTrue(core.get('appearance','large_font'))

    def test_two_processes_merge_distinct_keys_inside_transaction(self):
        store=Store(self.tmp/'b.db');store.close()
        script="""import sys
from pathlib import Path
from bender.core.store import Store
from bender.core.settings import Settings
db=Store(Path(sys.argv[1]));s=Settings(db);s.group('providers')
print('готов',flush=True);sys.stdin.readline()
for n in range(40):
 key=sys.argv[2]+'_'+str(n);s.set('providers',{key:{'id':key,'enabled':True}})
db.close()
"""
        processes=[subprocess.Popen([sys.executable,'-c',script,str(self.tmp/'b.db'),prefix],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for prefix in ('left','right')]
        try:
            for process in processes:self.assertEqual(process.stdout.readline().strip(),'готов')
            for process in processes:process.stdin.write('\n');process.stdin.flush()
            for process in processes:
                output,error=process.communicate(timeout=15)
                self.assertEqual(process.returncode,0,error+output)
            store=Store(self.tmp/'b.db')
            try:providers=Settings(store).group('providers')
            finally:store.close()
            self.assertEqual(set(providers),{prefix+'_'+str(n) for prefix in ('left','right') for n in range(40)})
        finally:
            for process in processes:
                if process.poll() is None:process.kill();process.communicate(timeout=5)
