"""Контексты стаи: память, роутер, откат, архив и возврат одного агента."""
import asyncio
import json
from unittest.mock import patch

from bender.core.errors import HumanError
from bender.core.store import Store
from bender.guard.policy import Action
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class AgentsTest(TempDirCase):
    @async_test
    async def test_private_memory_sessions_roles_paths_and_common_health(self):
        async with MindEnv(self.tmp) as env:
            host=env.app;host.agents.create('Второй',agent_id='second',awaken=False)
            other=host.agents.get('second');pid=host.models.roles()['main'][0]['provider']
            other.models.set_role('main',[{'provider':pid,'model':'echo'}])
            self.assertEqual(host.models.roles()['main'][0]['model'],'script')
            for ctx,text in ((host,'СЕКРЕТ-ПЕРВОГО-ОГОРОД'),(other,'СЕКРЕТ-ВТОРОГО-ОКЕАН')):
                (ctx.dirs.diary/'test.md').write_text(text);ctx.index.sync_files()
                self.assertTrue(ctx.index.search(text))
            self.assertFalse(other.index.search('СЕКРЕТ-ПЕРВОГО-ОГОРОД'))
            self.assertIsNone(other.store.session(env.sid))
            self.assertIs(other.router.health,host.router.health)
            response=await other.mind.say(other.main_session(),'привет')
            self.assertIn('Эхо:',response['text']);self.assertIsNone(response['error'])
            d=await other.guard.check(Action('fs.read',str(host.dirs.diary/'test.md')))
            self.assertFalse(d.allowed)
            self.assertTrue(host.journal.verify()['ok'])
            self.assertTrue(all(r['data'].get('agent_id','main')=='second' for r in other.journal.recent()))
            from bender.agents.runtime import ACTIVE
            from bender.mind.passport import snapshot
            token=ACTIVE.set(other)
            try:host.fuel.observe(other.router.chain('main')[0],0,'error',{'http':429,'class':'rate_limit','body':'ЛИЧНЫЙ-ТЕКСТ-ВТОРОГО'})
            finally:ACTIVE.reset(token)
            self.assertNotIn('ЛИЧНЫЙ-ТЕКСТ-ВТОРОГО',json.dumps(snapshot(host),ensure_ascii=False))
            self.assertIn('ЛИЧНЫЙ-ТЕКСТ-ВТОРОГО',json.dumps(snapshot(other),ensure_ascii=False))
            host.settings.set('flock',{'enabled':False})
            self.assertFalse(other.heart.enabled())
            with self.assertRaises(HumanError):await other.mind.say(other.main_session(),'не запускай')

    @async_test
    async def test_export_import_keeps_main_and_archive_then_delete_has_backup(self):
        async with MindEnv(self.tmp) as env:
            host=env.app;before=host.selfdir.read('MEMORY.md')
            host.agents.create('Один',agent_id='one',awaken=False)
            ctx=host.agents.get('one');ctx.selfdir.write('MEMORY.md','# Своя память\n\nЗАПОМНЕННЫЙ-ЛИМОН')
            (ctx.dirs.work/'file.txt').write_text('свой файл')
            ctx.settings.set('appearance',{'large_font':True})
            raw=host.agents.export('one')
            imported=host.agents.import_archive(raw,name='Копия');copy=host.agents.get(imported['id'])
            self.assertIn('ЗАПОМНЕННЫЙ',copy.selfdir.read('MEMORY.md'))
            self.assertEqual((copy.dirs.work/'file.txt').read_text(),'свой файл')
            self.assertTrue(copy.settings.get('appearance','large_font'))
            self.assertEqual(host.selfdir.read('MEMORY.md'),before)
            await host.agents.update('one',archived=True)
            with self.assertRaises(HumanError):host.agents.get('one')
            await host.agents.update('one',archived=False)
            result=await host.agents.delete('one',confirmed=True)
            from pathlib import Path
            self.assertTrue(Path(result['backup']).is_file())
            with self.assertRaises(HumanError):host.agents.create('Новый',agent_id='one')
            old=Store(host.dirs.db)
            try:
                self.assertIsNone(old.session(copy.main_session()))
                self.assertEqual(old.session(env.sid)['agent_id'],'main')
            finally:old.close()

    @async_test
    async def test_global_unattended_lane_is_shared_and_stops(self):
        async with MindEnv(self.tmp) as env:
            host=env.app;host.agents.create('Два',agent_id='two',awaken=False);ctx=host.agents.get('two')
            host.agents.background_limit=asyncio.Semaphore(1)
            started=asyncio.Event();release=asyncio.Event();current=0;peak=0
            original=host.router.call
            async def hold(*args,**kwargs):
                nonlocal current,peak
                current+=1;peak=max(peak,current);started.set()
                await release.wait()
                try:return await original(*args,**kwargs)
                finally:current-=1
            env.script([{'text':'первый'},{'text':'второй'}])
            with patch.object(host.router,'call',hold):
                one=asyncio.create_task(host.mind.say(env.sid,'один',actor='schedule',unattended=True))
                await started.wait()
                two=asyncio.create_task(ctx.mind.say(ctx.main_session(),'два',actor='schedule',unattended=True))
                await asyncio.sleep(.02);self.assertEqual(current,1);release.set()
                await asyncio.gather(one,two)
            self.assertEqual(peak,1)

    @async_test
    async def test_agent_cannot_read_control_api_even_through_redirect(self):
        from bender.net.http_server import HttpServer,JSONResponse,Response
        from tests.helpers import owner_web as create_web, owner_headers
        from bender.tools.registry import ToolContext
        async with MindEnv(self.tmp) as env:
            host=env.app;host.agents.create('Второй',agent_id='second',awaken=False)
            server=HttpServer(create_web(host,allowed_hosts={'127.0.0.1'}));await server.start('127.0.0.1',0)
            host.web_port=server.port
            try:
                own='http://127.0.0.1:'+str(server.port)+'/api/self'
                @env.fake.route('GET','/redirect-to-control')
                async def redirect(req):return Response(status=302,headers={'location':own})
                ctx=host.agents.get('second')
                for url in (own,env.base+'/redirect-to-control'):
                    result=await ctx.tools.run('web_fetch',{'url':url},ToolContext(ctx,session_id=ctx.main_session()))
                    self.assertFalse(result.ok,result.content)
                    self.assertIn('Служебный порт',result.content)
            finally:await server.stop()
