"""Самопроверка не чинит данные молча, отчёт исключает личное по белому списку."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from bender.core.app import App
from bender.core.errors import HumanError
from bender.diagnostics.doctor import collect,probe_models
from bender.diagnostics.report import build,preview,save,FILES
from bender.net.http_server import HttpServer
from tests.helpers import owner_web as create_web, owner_headers
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv
from tests.test_web_memory import _call


class DiagnosticsTest(TempDirCase):
    def test_passive_missing_data_and_preview_do_not_create_files(self):
        root=self.tmp/'absent'
        self.assertTrue(collect(root)['ok'])
        raw,plan=build(root)
        self.assertEqual(len(plan['files']),5)
        self.assertFalse(root.exists())
        for command in ('doctor','report'):
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            result=subprocess.run([sys.executable,'-m','bender','--data',str(root),command],input=b'',capture_output=True,timeout=10,cwd=self.tmp,env=env)
            self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(root.exists())
        self.assertEqual(list(self.tmp.glob('*.zip')),[])
        for limit in (0,501,'100'):
            with self.assertRaises(HumanError):preview(limit)

    @async_test
    async def test_corruption_is_explained_without_replacing_bytes(self):
        app=App(self.tmp/'data');await app.close();root=app.dirs.root
        cases=[('bender.db',b'broken','database'),('secrets.json',b'{','secrets'),('self/IDENTITY.md',b'','identity'),('local/boot.txt',b'bad','watchdog')]
        for path,broken,key in cases:
            with self.subTest(path=path):
                file=root/path;old=file.read_bytes() if file.exists() else None
                file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(broken)
                rows={row['id']:row for row in collect(root)['rows']}
                self.assertEqual(rows[key]['level'],'error',rows)
                raw,_=build(root);self.assertTrue(zipfile.is_zipfile(io.BytesIO(raw)))
                self.assertEqual(file.read_bytes(),broken)
                if old is None:file.unlink()
                else:file.write_bytes(old)
        app=App(root)
        app.store.x("INSERT OR REPLACE INTO settings(grp,value,updated_at) VALUES('web','[]',0)")
        await app.close()
        self.assertEqual(next(row for row in collect(root)['rows'] if row['id']=='settings')['level'],'error')

    @async_test
    async def test_report_omits_secrets_addresses_text_paths_and_unknown_settings(self):
        app=App(self.tmp/'private-person-data')
        secret='fixture-super-secret-72691';private='Личный рассказ хозяина 839104';address='owner.private@example.invalid'
        try:
            app.secrets.set('fixture',secret)
            sid=app.main_session();app.store.add_message(sid,{'role':'user','content':private+secret+address})
            app.journal.write('decision',private,actor=address,data={'capability':'net.send','target':address,'verdict':'ask','text':private,'token':secret,'count':2})
            app.journal.write('other',secret,actor=private,data={'capability':[]})
            app.store.x("INSERT OR REPLACE INTO settings(grp,value,updated_at) VALUES('future',?,0)",(json.dumps({'private':private+secret+address}),))
            app.settings.set('mail',{'owner_addresses':[address]})
            crash=app.dirs.root/'local'/'last_crash.txt'
            crash.write_text('Traceback '+private+'\n  File "'+str(Path(__file__).resolve().parents[1]/'bender/core/app.py')+'", line 24\n'+secret+' '+address+'\nValueError: '+private)
            raw,plan=build(app.dirs.root)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                self.assertEqual(set(archive.namelist()),set(FILES))
                text=''.join(archive.read(name).decode() for name in archive.namelist())
                for value in (secret,private,address,str(self.tmp),'fixture','owner_addresses','future'):
                    self.assertNotIn(value,text)
                diagnostics=json.loads(archive.read('doctor.json'))
                runtime=next(row for row in diagnostics['rows'] if row['id']=='runtime')
                self.assertIn('python',runtime['details']);self.assertIn('os',runtime['details'])
                self.assertIn('ValueError',text);self.assertIn('app.py',text);self.assertIn('net.send',text)
                for name in archive.namelist():
                    if name.endswith('.json'):json.loads(archive.read(name))
            target=self.tmp/'report.zip';save(app.dirs.root,target,raw)
            self.assertEqual(target.read_bytes(),raw)
            if os.name=='posix':self.assertEqual(target.stat().st_mode&0o777,0o600)
            original=(app.dirs.root/'secrets.json').read_bytes()
            with self.assertRaises(HumanError):save(app.dirs.root,app.dirs.root/'secrets.json',raw)
            self.assertEqual((app.dirs.root/'secrets.json').read_bytes(),original)
        finally:await app.close()

    @async_test
    async def test_explicit_model_probe_has_tiny_input_all_role_protocols_and_accounting(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;provider=app.models.roles()['main'][0]['provider']
            for role,model in (('ears','ears'),('voice','voice'),('embed','embedding')):
                app.models.set_role(role,[{'provider':provider,'model':model}])
            before=len(env.fake.state.requests)
            collect(app.dirs.root,app=app)
            self.assertEqual(len(env.fake.state.requests),before)
            app.settings.set('voice',{'enabled':True,'format':'wav'})
            rows=await probe_models(app)
            self.assertTrue(all(row['level']=='ok' for row in rows),rows)
            self.assertEqual(len(rows),4)
            self.assertGreaterEqual(app.store.one('SELECT count(*) n FROM calls')['n'],4)
            self.assertNotIn('doctor',app.guard._turn_grants)
            self.assertEqual(app.store.one('SELECT count(*) n FROM messages')['n'],0)
            bodies=[r['body'] for r in env.fake.state.requests if isinstance(r.get('body'),dict) and 'messages' in r['body']]
            self.assertEqual(bodies[0]['max_tokens'],8)
            self.assertEqual(len(bodies[0]['messages']),1)

    @async_test
    async def test_api_report_requires_review_and_csrf(self):
        async with MindEnv(self.tmp) as env:
            web=HttpServer(create_web(env.app,allowed_hosts={'127.0.0.1'}));await web.start('127.0.0.1',0)
            try:
                status,result=await asyncio.to_thread(_call,web.port,'/api/doctor')
                self.assertEqual(status,200);self.assertIn('rows',result)
                status,result=await asyncio.to_thread(_call,web.port,'/api/report',{})
                self.assertEqual(status,400);self.assertEqual(result['error']['code'],'report_preview')
                from urllib.request import Request,build_opener,ProxyHandler
                from urllib.error import HTTPError
                def download(csrf):
                    req=Request('http://127.0.0.1:'+str(web.port)+'/api/report',data=b'{"reviewed":true}',headers=owner_headers(**({'X-Bender':'1'} if csrf else {})))
                    try:
                        with build_opener(ProxyHandler({})).open(req,timeout=10) as response:return response.status,response.read()
                    except HTTPError as exc:return exc.code,exc.read()
                self.assertEqual((await asyncio.to_thread(download,False))[0],403)
                status,raw=await asyncio.to_thread(download,True)
                self.assertEqual(status,200);self.assertTrue(zipfile.is_zipfile(io.BytesIO(raw)))
            finally:await web.stop()
