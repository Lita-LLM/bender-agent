"""Поручение — приватный агент: уборка, отчёт, артефакты, память только на чтение."""
import asyncio
from unittest.mock import patch
from bender.agents.temporary import keep
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv,call


class TemporaryAgentsTest(TempDirCase):
    @async_test
    async def test_foreign_tool_callbacks_share_pause_and_restore_one_slot(self):
        from bender.agents.manager import BACKGROUND
        from bender.tools.registry import ToolContext
        async with MindEnv(self.tmp) as env:
            app=env.app;app.models.set_role('fast',app.models.roles()['main'])
            app.agents.background_limit=asyncio.Semaphore(1);env.script([{'text':'Готово'}])
            async with app.agents.background():
                lease=BACKGROUND.get()
                task=app.team.spawn('reader','Из MCP-моста',parent_session=env.sid,unattended=True)
                ctx=ToolContext(app,session_id=env.sid,turn={'_background_lease':lease})
                results=await asyncio.gather(*(asyncio.create_task(app.tools.run('wait_tasks',{'ids':[task['id']]},ctx)) for _ in range(2)))
                self.assertTrue(all(r.ok for r in results));self.assertTrue(lease.held);self.assertEqual(lease.pauses,0)
            self.assertEqual(app.agents.background_limit._value,1)

    @async_test
    async def test_single_global_background_slot_yields_while_waiting_for_child(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.settings.set('memory',{'review':False,'compact':False})
            app.models.set_role('fast',app.models.roles()['main'])
            app.agents.background_limit=asyncio.Semaphore(1)
            waiting=call('wait_tasks',ids=[])
            env.script([call('spawn_task',role='reader',goal='Прочитай'),waiting,{'text':'Прочитал'},{'text':'Родитель дождался'}])
            def identify(event):
                if event['type']=='task' and event['task']['status']=='queued':waiting['tool_calls'][0]['arguments']['ids']=[event['task']['id']]
            app.bus.listen(identify)
            active=0;peak=0;original=app.router.call
            async def measured(*args,**kwargs):
                nonlocal active,peak
                active+=1;peak=max(peak,active)
                try:
                    await asyncio.sleep(.01)
                    return await original(*args,**kwargs)
                finally:active-=1
            with patch.object(app.router,'call',measured):
                result=await app.mind.say(env.sid,'Делегируй и дождись',actor='schedule',unattended=True)
            self.assertIsNone(result['error'],result);self.assertEqual(result['text'],'Родитель дождался',result);self.assertEqual(peak,1)

    @async_test
    async def test_private_work_cleanup_and_keep_last_rights_and_files(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.settings.set('memory',{'review':False})
            env.script([call('write_file',path='result.txt',content='Оригинал задачи'),{'text':'Готово: result.txt'}])
            task=app.team.spawn('coder','Подготовь файл',parent_session=env.sid)
            [done]=await app.team.wait([task['id']],10)
            self.assertEqual(done['status'],'done',done['error'])
            worker=done['worker_agent'];self.assertTrue(worker)
            self.assertNotIn(worker,app.agents.loaded)
            self.assertFalse((app.dirs.root/'agents'/worker).exists())
            self.assertEqual((app.dirs.work/'result.txt').read_text(),'Оригинал задачи')
            self.assertTrue(app.store.messages(done['session_id']));self.assertGreaterEqual(done['steps'],2)
            self.assertEqual(len(app.agents.public()),1)
            self.assertEqual(app.store.one('SELECT count(*) AS n FROM "agent_'+worker+'__messages"')['n'],0)
            # Оставляется снимок результата, а не изменённый позже файл родителя.
            (app.dirs.work/'result.txt').write_text('Позже изменено')
            record=await keep(app.team,task['id']);ctx=app.agents.get(record['id'])
            self.assertEqual((ctx.dirs.work/'result.txt').read_text(),'Оригинал задачи')
            self.assertEqual(app.agents.rights.effective(ctx)['capabilities'],done['worker_rights']['capabilities'])
            self.assertTrue(ctx.store.messages(ctx.main_session()))
            self.assertEqual((await keep(app.team,task['id']))['id'],ctx.agent_id)

    @async_test
    async def test_parent_memory_permission_is_read_only_and_revocable(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.settings.set('memory',{'review':False})
            (app.dirs.diary/'test.md').write_text('ПРИВАТНЫЙ-РОДИТЕЛЬ-ЛИМОН');app.index.sync_files()
            before=app.selfdir.read('MEMORY.md')
            app.team.save_role('copyist',{'title':'Копиист','prompt':'Читай','model':'main','capabilities':['fs.read','fs.write'],'read_parent_memory':True})
            env.script([call('recall',query='ПРИВАТНЫЙ-РОДИТЕЛЬ-ЛИМОН'),call('write_file',path=str(app.dirs.self_dir/'MEMORY.md'),content='Подмена'),{'text':'Только прочитал'}])
            task=app.team.spawn('copyist','Вспомни',parent_session=env.sid)
            [done]=await app.team.wait([task['id']],10)
            self.assertEqual(done['status'],'done',done['error'])
            tools=[m for m in app.store.messages(done['session_id']) if m['role']=='tool']
            self.assertIn('ПРИВАТНЫЙ-РОДИТЕЛЬ-ЛИМОН',tools[0]['content']);self.assertTrue(tools[0]['meta']['ok'])
            self.assertFalse(tools[1]['meta']['ok']);self.assertEqual(app.selfdir.read('MEMORY.md'),before)
            app.team.save_role('copyist',{'read_parent_memory':False})
            env.script([call('recall',query='ПРИВАТНЫЙ-РОДИТЕЛЬ-ЛИМОН'),{'text':'Доступа нет'}])
            task=app.team.spawn('copyist','Нельзя вспомнить',parent_session=env.sid)
            [done]=await app.team.wait([task['id']],10)
            tools=[m for m in app.store.messages(done['session_id']) if m['role']=='tool']
            self.assertFalse(tools[0]['meta']['ok']);self.assertNotIn('ПРИВАТНЫЙ-РОДИТЕЛЬ-ЛИМОН',tools[0]['content'])

    @async_test
    async def test_cancel_cleans_worker_and_role_template_intersects_parent(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;started=asyncio.Event()
            async def slow(*args,**kwargs):started.set();await asyncio.Event().wait()
            with patch.object(app.router,'call',slow):
                task=app.team.spawn('reader','Жди',parent_session=env.sid)
                await started.wait();active=app.team.get(task['id']);worker=active['worker_agent']
                self.assertTrue((app.dirs.root/'agents'/worker).exists())
                app.team.cancel(task['id']);[done]=await app.team.wait([task['id']],10)
                self.assertEqual(done['status'],'cancelled');self.assertFalse((app.dirs.root/'agents'/worker).exists())
            app.agents.create('Помощник',agent_id='helper',awaken=False)
            parent=app.agents.get('helper');record=app.agents.create_role(parent,'coder',awaken=False)
            ctx=app.agents.get(record['id']);rights=app.agents.rights.effective(ctx)
            self.assertNotIn('exec',rights['capabilities']);self.assertNotIn('run',rights['tools'])
