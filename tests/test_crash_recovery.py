"""Настоящий SIGKILL ядра в четырёх точках и восстановление настоящим Сторожем."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from bender.core.app import App
from bender.core.paths import PROGRAM_DIR
from bender.core.watchdog import load_boot
from bender.devtools.fake_llm import create_router
from bender.net.http_server import HttpServer
from tests.helpers import TempDirCase,async_test
from tests.test_watchdog import _api,_free_port,_API_DATA

HOOK = r'''
import os,time
from pathlib import Path
if os.environ.get('BENDER_CORE')=='1':
 root=Path(os.environ['BENDER_TEST_CRASH'])
 def pause(mode):
  if (root/'armed').exists() and (root/'mode').read_text()==mode:
   (root/'ready').write_text(str(os.getpid()))
   while (root/'armed').exists():time.sleep(.02)
 from bender.router.core import Switchboard
 original_call=Switchboard.call
 async def call(*args,**kwargs):
  pause('turn')
  return await original_call(*args,**kwargs)
 Switchboard.call=call
 from bender.core import fsutil
 original_replace=fsutil.os.replace
 def replace(source,target,*args,**kwargs):
  if Path(target).name=='MEMORY.md':pause('memory')
  return original_replace(source,target,*args,**kwargs)
 fsutil.os.replace=replace
 from bender.backups import manager as backups
 original_copy=backups.copy_object
 def copy(*args,**kwargs):
  pause('backup')
  return original_copy(*args,**kwargs)
 backups.copy_object=copy
 import shutil
 original_tree=shutil.copytree;original_file=shutil.copy2
 def file(source,target,*args,**kwargs):
  result=original_file(source,target,*args,**kwargs)
  pause('update')
  return result
 def tree(source,target,*args,**kwargs):
  if Path(target).name=='slot' and '.install-' in str(target):kwargs['copy_function']=file
  return original_tree(source,target,*args,**kwargs)
 shutil.copytree=tree
'''


class CrashRecoveryTest(TempDirCase):
    @async_test
    async def test_kill_during_turn_memory_backup_and_update(self):
        if os.name!='posix':self.skipTest('Нужен SIGKILL')
        router=create_router();fake=HttpServer(router);await fake.start('127.0.0.1',0)
        hook=self.tmp/'hook';hook.mkdir();(hook/'sitecustomize.py').write_text(HOOK)
        try:
            for mode in ('turn','memory','backup','update'):
                with self.subTest(mode=mode):
                    root=self.tmp/mode;root.mkdir();data=root/'data';app=App(data)
                    provider=app.models.add_provider(name='Фальшивка',kind='openai',base_url=f'http://127.0.0.1:{fake.port}/v1',extra={'is_local':True})
                    app.models.set_role('main',[{'provider':provider['id'],'model':'script'}])
                    app.settings.set('guard',{'profile':'trust'})
                    app.settings.set('memory',{'review':False})
                    app.settings.set('backups',{'path':str(root/'disk')})
                    app.settings.set('updates',{'allow_development':True})
                    app.selfdir.finish_awakening();sid=app.main_session();old_memory=app.selfdir.read('MEMORY.md')
                    await app.backups.now()
                    record=app.versions.stage(PROGRAM_DIR,development=True) if mode=='update' else None
                    await app.close()
                    port=_free_port();_API_DATA[port]=data;log=(root/'process.log').open('wb')
                    env={**os.environ,'PYTHONPATH':str(hook)+os.pathsep+str(PROGRAM_DIR),'BENDER_TEST_CRASH':str(root)}
                    for key in ('BENDER_CORE','BENDER_WATCHED','BENDER_SAFE','BENDER_RESTARTED','BENDER_TRIAL'):env.pop(key,None)
                    process=subprocess.Popen([sys.executable,'-m','bender','--data',str(data),'run','--port',str(port),'--no-browser'],cwd=PROGRAM_DIR,env=env,stdout=log,stderr=log)
                    async def until(predicate,timeout=12):
                        deadline=time.monotonic()+timeout
                        while time.monotonic()<deadline:
                            value=predicate()
                            if value:return value
                            await asyncio.sleep(.05)
                        self.fail('Не дождались: '+mode+'; '+(root/'process.log').read_text()[-1000:])
                    def ready():
                        try:return _api(port,'/api/state')
                        except OSError:return False
                    pending=None
                    try:
                        await until(ready)
                        starts=load_boot(data)['starts'];(root/'mode').write_text(mode);(root/'armed').touch()
                        if mode in ('turn','memory'):
                            router.state.script=[{'text':'Ответ'}] if mode=='turn' else [{'tool_calls':[{'name':'memory','arguments':{'action':'add','text':'Новая запись при обкатке'}}]}]
                            await asyncio.to_thread(_api,port,'/api/say',{'text':'Проверь память','session':sid},2)
                        elif mode=='backup':pending=asyncio.create_task(asyncio.to_thread(_api,port,'/api/backups/now',{},15))
                        else:pending=asyncio.create_task(asyncio.to_thread(_api,port,f"/api/versions/{record['id']}/install",{'hash':record['hash']},15))
                        if mode=='update':
                            cards=await until(lambda:(ready() or {}).get('pending'))
                            await asyncio.to_thread(_api,port,'/api/approvals/'+cards[0]['id'],{'choice':'once'},2)
                        await until(lambda:(root/'ready').exists())
                        pid=int((root/'ready').read_text());os.kill(pid,signal.SIGKILL)
                        (root/'armed').unlink()
                        if pending:await asyncio.gather(pending,return_exceptions=True)
                        await until(lambda:load_boot(data).get('starts',0)>starts and ready())
                        self.assertIsNone(process.poll())
                        db=sqlite3.connect(data/'bender.db')
                        try:
                            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
                            if mode in ('turn','memory','backup','update'):
                                rows=_api(port,f'/api/session/{sid}/messages')
                                self.assertIn('прерван',json.dumps(rows,ensure_ascii=False).lower())
                        finally:db.close()
                        self.assertEqual((data/'self'/'MEMORY.md').read_text(),old_memory)
                        if mode=='backup':
                            response=await asyncio.to_thread(_api,port,'/api/backups/verify',{'full':True},5)
                            self.assertTrue(response['ok'],response)
                        if mode=='update':
                            self.assertFalse((data/'versions'/record['id']).exists())
                            self.assertFalse(load_boot(data).get('slots',{}).get('pending'))
                    finally:
                        process.terminate()
                        try:await asyncio.to_thread(process.wait,timeout=5)
                        except subprocess.TimeoutExpired:process.kill();await asyncio.to_thread(process.wait)
                        if pending:await asyncio.gather(pending,return_exceptions=True)
                        log.close()
        finally:await fake.stop()
