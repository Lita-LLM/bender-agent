"""Настоящий localhost и bubblewrap: сеть открыта, кука и чужие файлы закрыты."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from unittest.mock import patch

from bender.core.errors import HumanError
from bender.core.web_auth import headers, token
from bender.guard import sandbox
from bender.guard.policy import Action
from bender.net.http_server import HttpServer, Response
from bender.tools.registry import ToolContext
from bender.web.api import create_web
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class AcceptanceSecurityTest(TempDirCase):
    @async_test
    async def test_ssh_aliases_never_open_a_key_or_service_data_for_writing(self):
        async with MindEnv(self.tmp) as env:
            home=self.tmp/'ssh-home';ssh=home/'.ssh';ssh.mkdir(parents=True)
            key=ssh/'id_test';key.write_text('ФАЛЬШИВЫЙ-КЛЮЧ');known=ssh/'known_hosts'
            with patch.dict(os.environ,{'HOME':str(home)}),patch.object(sandbox,'detect',return_value={'kind':'bwrap','path':'/usr/bin/bwrap','fs':True,'net':True,'title':'тест'}):
                for link in ('symlink','hardlink'):
                    known.symlink_to(key) if link=='symlink' else os.link(key,known)
                    try:
                        with self.assertRaises(HumanError) as denied:
                            sandbox.prepare('true',info=sandbox.for_app(env.app),work_dir=env.app.dirs.work,allow_net=True)
                        self.assertEqual(denied.exception.code,'ssh_hosts')
                    finally:known.unlink()
                key.unlink();ssh.rmdir();ssh.symlink_to(env.app.dirs.root,target_is_directory=True)
                with self.assertRaises(HumanError) as denied:
                    sandbox.prepare('true',info=sandbox.for_app(env.app),work_dir=env.app.dirs.work,allow_net=True)
                self.assertEqual(denied.exception.code,'ssh_path')

    @async_test
    async def test_owner_git_update_module_and_network_mcp_with_two_agents(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.agents.create('Второй',agent_id='second',awaken=False)
            app.settings.set('guard',{'profile':'off'})
            app.settings.set('updates',{'allow_development':True})
            bindir=app.dirs.work/'bin';bindir.mkdir()
            source=app.dirs.work/'module-source'
            shutil.copytree(Path(__file__).parent/'fixtures/module_demo',source)
            fake=bindir/'git'
            fake.write_text('#!'+sys.executable+'\n'+'''import pathlib,sys,zipfile,tarfile
if 'clone' in sys.argv:pathlib.Path(sys.argv[-1]).mkdir()
else:
 out=next(x.split('=',1)[1] for x in sys.argv if x.startswith('--output='))
 if out.endswith('.zip'):
  with zipfile.ZipFile(out,'w') as z:
   for p in ('bender/__init__.py','bender/__main__.py','bender/core/server_main.py'):z.writestr(p,'# проверка')
 else:
  with tarfile.open(out,'w') as t:
   for p in pathlib.Path(%r).iterdir():t.add(p,arcname=p.name)
''' % str(source));fake.chmod(0o700)
            owner=ToolContext(app,actor='owner',session_id='owner-install')
            with patch.dict(os.environ,{'PATH':str(bindir)+os.pathsep+os.environ['PATH']}):
                update=await app.versions.stage_git('https://example.invalid/program.git',owner)
                self.assertEqual(update['files'],3)
                module=await app.modules.import_git('https://example.invalid/module.git',owner)
                self.assertEqual(module['id'],'demo')
            state=app.dirs.work/'mcp-state.json';state.write_text('{}')
            await app.mcp.save({'id':'network','transport':'stdio','command':[sys.executable,str(Path(__file__).parent/'fixtures/fake_mcp.py'),str(state)],'cwd':str(app.dirs.work),'network':True,'enabled':True})
            await app.mcp.discover('network',owner)
            self.assertTrue(app.mcp.observed['network'])
            other=app.agents.get('second')
            result=await other.tools.run('run',{'command':'echo owner-operation','allow_net':True},ToolContext(other,actor='owner'))
            self.assertTrue(result.ok,result.content)

    @async_test
    async def test_localhost_requires_cookie_for_page_reads_writes_and_sse(self):
        async with MindEnv(self.tmp) as env:
            app=env.app
            web=HttpServer(create_web(app,allowed_hosts={'127.0.0.1'}))
            await web.start('127.0.0.1',0)
            base='http://127.0.0.1:'+str(web.port)
            try:
                for path in ('/','/api/state','/api/events'):
                    response=await app.http.get(base+path)
                    async with response:self.assertEqual(response.status,401,await response.read())
                response=await app.http.post(base+'/api/settings/guard',json={'enabled':False},headers={'X-Bender':'1'})
                async with response:self.assertEqual(response.status,401)
                self.assertTrue(app.settings.get('guard','enabled'))
                response=await app.http.get(base+'/?token='+token(app.dirs.root))
                async with response:
                    self.assertEqual(response.status,302)
                    self.assertIn('HttpOnly',response.headers.get('set-cookie'))
                    self.assertEqual(response.headers.get('location'),'/')
                response=await app.http.get(base+'/api/state',headers=headers(app.dirs.root))
                async with response:self.assertEqual(response.status,200)
                response=await app.http.post(base+'/api/settings/guard',json={'profile':'trust'},headers=headers(app.dirs.root))
                async with response:self.assertEqual(response.status,200)
                if os.name=='posix':self.assertEqual((app.dirs.local/'web_token').stat().st_mode&0o777,0o600)
            finally:await web.stop()

    @async_test
    async def test_shared_network_reaches_http_but_not_neighbor_or_control(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.agents.create('Сосед',agent_id='neighbor',awaken=False)
            neighbor=app.agents.get('neighbor');secret=neighbor.dirs.work/'private.txt';secret.write_text('СЕКРЕТ-СОСЕДА')
            info=sandbox.for_app(app)
            if info['kind']!='bwrap':self.skipTest('Нужен настоящий bubblewrap')
            web=HttpServer(create_web(app,allowed_hosts={'127.0.0.1'}));await web.start('127.0.0.1',0)
            app.settings.set('guard',{'profile':'off'})
            code='''import urllib.request, urllib.error, pathlib
print(urllib.request.urlopen(%r).read().decode())
print(pathlib.Path(%r).exists(), pathlib.Path(%r).exists())
for method,path in [('GET','state'),('POST','settings/guard')]:
 try:
  req=urllib.request.Request(%r+'/api/'+path,data=b'{"enabled":false}' if method=='POST' else None,headers={'X-Bender':'1'})
  urllib.request.urlopen(req)
 except urllib.error.HTTPError as exc:print(exc.code)
''' % (env.base+'/network-ok',str(secret),str(app.dirs.local/'web_token'),'http://127.0.0.1:'+str(web.port))
            @env.fake.route('GET','/network-ok')
            async def okay(req):return Response('СЕТЬ-РАБОТАЕТ')
            try:
                result=await app.tools.run('run',{'command':shlex.join([sys.executable,'-c',code]),'allow_net':True},ToolContext(app,session_id=env.sid))
                self.assertTrue(result.ok,result.content)
                self.assertIn('СЕТЬ-РАБОТАЕТ\nFalse False\n401\n401',result.content)
            finally:await web.stop()

    @async_test
    async def test_templates_fallback_owner_and_trust_are_explicit(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.agents.create('Помощник',agent_id='helper',awaken=False)
            ctx=app.agents.get('helper')
            with patch.object(sandbox,'detect',return_value={'kind':'none','fs':False,'net':False,'title':'Нет изоляции'}):
                full=sandbox.for_app(app)
                self.assertFalse(full['agent_isolation'])
                self.assertIn('видят файлы других агентов',full['title'])
                limited=sandbox.for_app(ctx)
                with self.assertRaises(HumanError) as denied:
                    sandbox.prepare('true',info=limited,work_dir=ctx.dirs.work,allow_net=True)
                self.assertIn('Стая → Права',denied.exception.hint)
                self.assertFalse(sandbox.for_app(ctx,owner=True)['agent_isolation'])
                ctx.settings.set('agent_rights',{'require_isolation':False},by='owner')
                self.assertFalse(sandbox.for_app(ctx)['agent_isolation'])
            for profile in ('trust','off'):
                app.settings.set('guard',{'profile':profile})
                for cap,target in (('exec','ssh -o BatchMode=yes example.invalid uptime'),('net.read','https://example.invalid')):
                    self.assertTrue((await app.guard.check(Action(cap,target,in_sandbox=False))).allowed)

    @async_test
    async def test_ssh_keys_readonly_hosts_writable_and_socket_survives_private_tmp(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;home=self.tmp/'home';ssh=home/'.ssh';ssh.mkdir(parents=True)
            (ssh/'id_test').write_text('ФАЛЬШИВЫЙ-КЛЮЧ');(home/'.gnupg').mkdir();(home/'.gnupg/key').write_text('СКРЫТЬ')
            sock=self.tmp/'ssh-agent.sock'
            async def answer(reader,writer):
                writer.write(b'agent-ok');await writer.drain();writer.close();await writer.wait_closed()
            server=await asyncio.start_unix_server(answer,path=str(sock))
            try:
                with patch.dict(os.environ,{'HOME':str(home),'SSH_AUTH_SOCK':str(sock)}):
                    if sandbox.for_app(app)['kind']!='bwrap':self.skipTest('Нужен настоящий bubblewrap')
                    app.settings.set('guard',{'profile':'off'})
                    script='''import pathlib,socket,os
p=pathlib.Path(%r)
print(p.read_text())
try:p.write_text('НЕ ДОЛЖНО ЗАПИСАТЬСЯ')
except OSError:print('readonly')
with (p.parent/'known_hosts').open('a') as f:f.write('узел'+chr(10))
print(pathlib.Path(%r).exists())
s=socket.socket(socket.AF_UNIX);s.connect(os.environ['SSH_AUTH_SOCK']);print(s.recv(100).decode())
''' % (str(ssh/'id_test'),str(home/'.gnupg/key'))
                    result=await app.tools.run('run',{'command':shlex.join([sys.executable,'-c',script]),'allow_net':True},ToolContext(app))
                    self.assertTrue(result.ok,result.content);self.assertIn('readonly\nFalse\nagent-ok',result.content)
                    self.assertEqual((ssh/'known_hosts').read_text(),'узел\n')
                    app.settings.set('agent_rights',{'ssh':False},by='owner')
                    result=await app.tools.run('run',{'command':shlex.join([sys.executable,'-c',f'from pathlib import Path; print(Path({str(ssh/"id_test")!r}).exists())'])},ToolContext(app))
                    self.assertTrue(result.ok,result.content);self.assertIn('False',result.content)
                    # Даже сокет в открытой work закрыт, если хозяин снял право SSH.
                    exposed=app.dirs.work/'ssh-agent.sock'
                    socket_server=await asyncio.start_unix_server(answer,path=str(exposed))
                    try:
                        with patch.dict(os.environ,{'SSH_AUTH_SOCK':str(exposed)}):
                            check='import socket,os; print(repr(os.environ.get("SSH_AUTH_SOCK"))); s=socket.socket(socket.AF_UNIX)\ntry:s.connect('+repr(str(exposed))+'); print("ОБХОД")\nexcept OSError:print("закрыт")'
                            result=await app.tools.run('run',{'command':shlex.join([sys.executable,'-c',check]),'allow_net':True},ToolContext(app))
                        self.assertTrue(result.ok,result.content);self.assertIn("''\nзакрыт",result.content)
                    finally:socket_server.close();await socket_server.wait_closed()
            finally:server.close();await server.wait_closed()
