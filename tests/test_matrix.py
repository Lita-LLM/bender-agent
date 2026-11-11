"""Matrix через настоящий одобренный процесс: HTTPS, привязка, файлы, реакции и границы."""
import asyncio
import base64
import json
import os
import ssl
from pathlib import Path
from unittest import mock

from bender.core.app import App
from bender.core.errors import HumanError
from bender.core.settings import NeedsOwner
from bender.net.http_server import HttpServer,Router,JSONResponse,Response
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test,owner_web,owner_headers

ROOM='!own:example.org';BOT='@bot:example.org';OWNER='@owner:example.org'
FIXTURES=Path(__file__).parent/'fixtures'


class MatrixTest(TempDirCase):
    async def setup_matrix(self,*,tls=False):
        self.app=App(self.tmp/'data');self.calls=[];self.members=[BOT,OWNER];self.encrypted=False;self.events=[];self.batch=0;self.messages=[];self.payload='ТЕСТОВЫЙ ФАЙЛ'.encode()
        router=Router()
        @router.route('GET','/_matrix/client/v3/account/whoami')
        async def who(req):self.calls.append(('who',req));return JSONResponse({'user_id':BOT})
        @router.route('POST','/_matrix/client/v3/join/{room}')
        async def join(req):self.calls.append(('join',req));return JSONResponse({'room_id':ROOM})
        @router.route('GET','/_matrix/client/v3/rooms/{room}/joined_members')
        async def members(req):self.calls.append(('members',req));return JSONResponse({'joined':{value:{} for value in self.members}})
        @router.route('GET','/_matrix/client/v3/rooms/{room}/state/m.room.encryption/')
        async def encryption(req):self.calls.append(('encryption',req));return JSONResponse({'algorithm':'m.megolm.v1.aes-sha2'} if self.encrypted else {},200 if self.encrypted else 404)
        @router.route('GET','/_matrix/client/v3/sync')
        async def sync(req):
            self.calls.append(('sync',req));self.batch+=1
            if hasattr(self,'sync_block'):
                self.sync_block[0].set();await self.sync_block[1].wait()
            events,self.events=self.events,[]
            return JSONResponse({'next_batch':str(self.batch),'rooms':{'join':{ROOM:{'timeline':{'events':events}}}}})
        @router.route('PUT','/_matrix/client/v3/rooms/{room}/send/{kind}/{txn}')
        async def send(req):
            self.calls.append(('send',req));self.messages.append({'kind':req.params['kind'],'content':req.json(),'txn':req.params['txn']})
            return JSONResponse({'event_id':'$sent'+str(len(self.messages))})
        @router.route('GET','/_matrix/client/v1/media/download/{server}/{media}')
        async def download(req):
            self.calls.append(('download',req))
            if getattr(self,'download_redirect',False):return Response(b'',302,{'Location':self.address+'/not-followed'})
            return Response(self.payload,content_type='text/plain')
        @router.route('POST','/_matrix/media/v3/upload')
        async def upload(req):self.calls.append(('upload',req));return JSONResponse({'content_uri':'mxc://example.org/abc_123'})
        self.server=HttpServer(router)
        if tls:
            ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(FIXTURES/'mcp-oauth-cert.pem',FIXTURES/'mcp-oauth-key.pem')
            self.server._server=await asyncio.start_server(self.server._accept,'127.0.0.1',0,ssl=ctx)
            self.server.host,self.server.port=self.server._server.sockets[0].getsockname()[:2]
        else:await self.server.start('127.0.0.1',0)
        self.address=('https' if tls else 'http')+f'://127.0.0.1:{self.server.port}'
        self.approvals=[]
        def answer(event):
            if event.get('type')=='approval':
                self.approvals.append(event['card']);asyncio.get_running_loop().call_soon(self.app.guard.respond,event['card']['id'],'once')
        self.app.bus.listen(answer)
        self.app.modules.import_bundled('matrix');package=self.app.modules.package('matrix')
        await self.app.modules.approve('matrix',package['hash']);sid=self.app.modules.trust('matrix')['server']
        await self.app.mcp.discover(sid,ToolContext(self.app,actor='owner'))
        self.app.mcp.approve(sid,next(row['review'] for row in self.app.mcp.public()['servers'] if row['id']==sid))
        await self.app.matrix.configure({'homeserver':self.address,'room_id':ROOM,'agent_id':'main'},'PRIVATE_MATRIX_TOKEN')
        self.app.settings.set('matrix',{'enabled':True},by='owner')

    async def shutdown(self):await self.app.close();await self.server.stop()

    def event(self,text,*,sender=OWNER,eid='$in1',content=None,kind='m.room.message'):
        return {'type':kind,'event_id':eid,'sender':sender,'content':content or {'msgtype':'m.text','body':text}}

    async def pair(self):
        await self.app.matrix.poll_once();self.app.matrix.state='unpaired';code=self.app.matrix.pair()['code']
        self.events=[self.event('/pair '+code,eid='$pair')];await self.app.matrix.poll_once()
        self.assertEqual(self.app.matrix.owner()['user_id'],OWNER)
        return self.app.matrix.selected()

    @async_test
    async def test_https_real_process_pair_message_delivery_and_no_history_replay(self):
        with mock.patch.dict(os.environ,{'SSL_CERT_FILE':str(FIXTURES/'mcp-oauth-cert.pem')}):
            await self.setup_matrix(tls=True)
            try:
                self.events=[self.event('СТАРОЕ ПОРУЧЕНИЕ')]
                with mock.patch.object(self.app.mind,'say',new=mock.AsyncMock()) as say:
                    ctx,sid=await self.pair();self.assertEqual(say.await_count,0)
                    self.events=[self.event('ТЕКУЩИЙ ВОПРОС',eid='$current')];await self.app.matrix.poll_once()
                    self.assertEqual(say.await_count,1);self.assertEqual(say.call_args.kwargs['actor'],'owner')
                    self.assertEqual(say.call_args.kwargs['channel'],'matrix');self.assertFalse(say.call_args.kwargs['tainted'])
                    self.events=[self.event('ПОВТОР',eid='$current')];await self.app.matrix.poll_once();self.assertEqual(say.await_count,1)
                self.app.matrix.stopping=False
                self.app.matrix.sender=asyncio.create_task(self.app.matrix.deliver())
                self.app.matrix.answer({'type':'answer','channel':'matrix','session':sid,'agent_id':'main','result':{'text':'ФИНАЛ'}})
                for _ in range(200):
                    if len(self.messages)>=2:break
                    await asyncio.sleep(.01)
                self.assertEqual(self.messages[-1]['content']['body'],'ФИНАЛ')
                self.assertTrue(all(req.headers.get('authorization')=='Bearer PRIVATE_MATRIX_TOKEN' for _,req in self.calls))
                self.assertTrue(all('PRIVATE_MATRIX_TOKEN' not in req.target for _,req in self.calls))
                sync=next(req for name,req in self.calls if name=='sync');self.assertEqual(sync.q('timeout'),'60000')
                self.assertEqual(json.loads(sync.q('filter'))['room']['rooms'],[ROOM])
                self.assertNotIn('PRIVATE_MATRIX_TOKEN',json.dumps(self.app.settings.group('matrix')))
                self.assertEqual(self.app.dirs.secrets.stat().st_mode&0o777,0o600)
            finally:await self.shutdown()

    @async_test
    async def test_files_are_untrusted_and_common_send_file_react_reply_use_matrix(self):
        await self.setup_matrix()
        try:
            ctx,sid=await self.pair()
            self.events=[self.event('документ.txt',eid='$file',content={'msgtype':'m.file','body':'документ.txt','url':'mxc://foreign.example/file_1'})]
            with mock.patch.object(ctx.mind,'say',new=mock.AsyncMock()) as say:await self.app.matrix.poll_once()
            self.assertTrue(say.call_args.kwargs['tainted']);items=say.call_args.kwargs['message_meta']['attachments']
            self.assertEqual(Path(items[0]['path']).read_bytes(),self.payload);self.assertEqual(items[0]['source'],'matrix')
            context=ToolContext(ctx,actor='agent',session_id=sid)
            reference=next(int(k) for k,v in ctx.store.meta_get('matrix:refs').items() if v['event_id']=='$file')
            result=await ctx.tools.run('react',{'message':reference,'emoji':'🧪'},context);self.assertTrue(result.ok,result.content)
            self.assertEqual(self.messages[-1]['kind'],'m.reaction');self.assertEqual(self.messages[-1]['content']['m.relates_to']['event_id'],'$file')
            result=await ctx.tools.run('send_message',{'text':'Ответ','reply_to':reference},context);self.assertTrue(result.ok,result.content)
            self.assertEqual(self.messages[-1]['content']['m.relates_to']['m.in_reply_to']['event_id'],'$file')
            outgoing=ctx.dirs.work/'out.txt';outgoing.write_text('От агента')
            result=await ctx.tools.run('send_file',{'path':str(outgoing)},context);self.assertTrue(result.ok,result.content)
            self.assertEqual(self.messages[-1]['content']['url'],'mxc://example.org/abc_123')
            self.assertTrue(any(name=='upload' and req.body==outgoing.read_bytes() for name,req in self.calls))
        finally:await self.shutdown()

    @async_test
    async def test_encryption_stranger_and_unrelated_session_block_network_sends(self):
        await self.setup_matrix()
        try:
            ctx,sid=await self.pair();initial=len(self.messages)
            self.encrypted=True
            with self.assertRaises(HumanError):await self.app.matrix.send(ToolContext(ctx,actor='owner',session_id=sid),'НЕ ОТПРАВЛЯТЬ')
            self.assertEqual(len(self.messages),initial)
            self.encrypted=False;self.members.append('@stranger:example.org')
            with self.assertRaises(HumanError):await self.app.matrix.poll_once()
            with self.assertRaises(HumanError):await self.app.matrix.send(ToolContext(ctx,actor='owner',session_id=sid),'НЕ ОТПРАВЛЯТЬ')
            self.assertEqual(len(self.messages),initial)
            self.members=[BOT,OWNER]
            with self.assertRaises(HumanError):await self.app.matrix.send(ToolContext(ctx,actor='owner',session_id=ctx.main_session()),'НЕ ТОТ ЧАТ')
            self.assertEqual(len(self.messages),initial)
        finally:await self.shutdown()

    @async_test
    async def test_agent_isolation_foreign_sender_and_role_ceiling(self):
        await self.setup_matrix()
        try:
            self.app.agents.create('Другой',agent_id='other',awaken=False)
            self.app.settings.set('matrix',{'agent_id':'other'},by='owner');ctx,sid=await self.pair()
            self.assertEqual(ctx.agent_id,'other')
            self.events=[self.event('ЧУЖОЙ',sender='@stranger:example.org',eid='$stranger'),self.event('СВОЙ',eid='$own')]
            with mock.patch.object(ctx.mind,'say',new=mock.AsyncMock()) as say:await self.app.matrix.poll_once()
            self.assertEqual(say.await_count,1);self.assertIn('СВОЙ',say.call_args.args[1])
            self.assertIsNone(self.app.store.session(sid))
            with mock.patch.object(ctx.team,'permissions',return_value={'capabilities':[],'tools':['send_message']}):
                result=await ctx.tools.run('send_message',{'text':'НЕЛЬЗЯ'},ToolContext(ctx,actor='subagent:test',session_id=sid))
            self.assertFalse(result.ok)
        finally:await self.shutdown()

    @async_test
    async def test_mxc_rejects_traversal_redirects_and_disabled_module(self):
        await self.setup_matrix()
        try:
            await self.pair()
            for uri in ('https://evil.example/file','mxc://example.org/../secret','mxc://evil/percent%2f','mxc://user:pass@evil/file'):
                with self.assertRaises(HumanError):await self.app.matrix.transport.call('download',{'uri':uri})
            self.assertFalse(any(name=='download' for name,_ in self.calls))
            await self.app.modules.set_enabled('matrix',False)
            with self.assertRaises(HumanError):await self.app.matrix.transport.call('status')
            self.assertFalse(self.app.matrix.transport.clients)
        finally:await self.shutdown()

    @async_test
    async def test_owner_api_auth_and_token_rotation_clear_binding(self):
        await self.setup_matrix();web=HttpServer(owner_web(self.app,allowed_hosts={'127.0.0.1','localhost'}));await web.start('127.0.0.1',0)
        try:
            await self.pair();url=f'http://127.0.0.1:{web.port}/api/channels/matrix/configure'
            response=await self.app.http.post(url,json={'token':'NEW_PRIVATE'},headers={'X-Bender':'1'})
            async with response:self.assertEqual(response.status,401);await response.read()
            self.assertEqual(self.app.secrets.get('matrix:token'),'PRIVATE_MATRIX_TOKEN')
            response=await self.app.http.post(url,json={'token':'NEW_PRIVATE'},headers=owner_headers(**{'X-Bender':'1'}))
            async with response:
                self.assertEqual(response.status,200);payload=json.loads(await response.read())
                self.assertFalse(payload['owner']);self.assertNotIn('NEW_PRIVATE',json.dumps(payload));self.assertFalse(payload['settings']['enabled'])
            with self.assertRaises(HumanError):self.app.settings.set('matrix',{'enabled':True},by='agent')
        finally:await web.stop();await self.shutdown()


    @async_test
    async def test_send_does_not_wait_for_long_poll_and_config_change_drops_old_input(self):
        await self.setup_matrix()
        try:
            ctx,sid=await self.pair();self.sync_block=(asyncio.Event(),asyncio.Event())
            self.events=[self.event('НЕ ИСПОЛНЯТЬ',eid='$stale')]
            pending=asyncio.create_task(self.app.matrix.poll_once())
            await self.sync_block[0].wait()
            await asyncio.wait_for(self.app.matrix.send(ToolContext(ctx,actor='owner',session_id=sid),'В ПОЛЁТЕ'),3)
            self.assertEqual(self.messages[-1]['content']['body'],'В ПОЛЁТЕ')
            self.app.settings.set('matrix',{'enabled':False},by='owner');self.sync_block[1].set()
            with mock.patch.object(ctx.mind,'say',new=mock.AsyncMock()) as say:
                with self.assertRaises(HumanError):await pending
                self.assertEqual(say.await_count,0)
            self.assertFalse(self.app.matrix.transport.clients)
        finally:await self.shutdown()

    @async_test
    async def test_idle_has_one_poll_per_minute_and_restarts_do_not_accumulate_listeners(self):
        await self.setup_matrix()
        try:
            matrix=self.app.matrix;count=len(self.app.bus._listeners)
            with mock.patch.object(matrix,'poll_once',new=mock.AsyncMock()) as poll:
                matrix.start();await asyncio.sleep(.03);self.assertEqual(poll.await_count,1)
                await asyncio.sleep(.05);self.assertEqual(poll.await_count,1)
                for _ in range(10):
                    await matrix.stop();matrix.start();await asyncio.sleep(.01)
                await matrix.stop()
                self.assertEqual(len(self.app.bus._listeners),count)
                self.assertFalse(matrix.transport.clients);self.assertIsNone(matrix.sender)
            self.assertGreaterEqual(matrix.settings()['poll_seconds'],60)
        finally:await self.shutdown()

    @async_test
    async def test_stop_does_not_reenter_empty_queue_when_delivery_consumes_cancellation(self):
        from bender.core.app import App
        from unittest.mock import patch
        self.app=App(self.tmp/'shutdown-race')
        channel=self.app.matrix;started=asyncio.Event()
        async def sending(*args,**kwargs):
            started.set()
            try:await asyncio.Future()
            except asyncio.CancelledError:return 1
        try:
            with patch.object(channel,'send',sending):
                channel.outbox.put_nowait((self.app,self.app.main_session(),'Уже отправляется',channel.identity()))
                channel.sender=asyncio.create_task(channel.deliver())
                await started.wait()
                await asyncio.wait_for(channel.stop(),1)
                self.assertIsNone(channel.sender);self.assertTrue(channel.outbox.empty())
        finally:await self.app.close()

    @async_test
    async def test_cancel_during_transport_cleanup_still_closes_real_process(self):
        from bender.mcp.client import Client
        await self.setup_matrix();started=asyncio.Event();release=asyncio.Event();clients=[];closed=[]
        original=Client.close
        async def delayed(client):
            clients.append(client);started.set();await release.wait()
            await original(client);closed.append(client)
        try:
            with mock.patch.object(Client,'close',delayed):
                task=asyncio.create_task(self.app.matrix.transport.call('status'))
                await started.wait();task.cancel();await asyncio.sleep(0);release.set()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertEqual(len(closed),1)
                self.assertIsNone(clients[0].transport.process)
                self.assertFalse(self.app.matrix.transport.clients)
        finally:
            release.set()
            for client in clients:await original(client)
            await self.shutdown()

    @async_test
    async def test_stop_does_not_sleep_after_poll_consumes_cancellation(self):
        from unittest.mock import patch
        self.app=App(self.tmp/'poll-stop-race');channel=self.app.matrix;started=asyncio.Event()
        async def polling():
            started.set()
            try:await asyncio.Future()
            except asyncio.CancelledError:return
        try:
            self.app.settings.set('matrix',{'enabled':True})
            with patch.object(channel,'poll_once',polling):
                channel.start();await started.wait()
                await asyncio.wait_for(channel.stop(),1)
                self.assertIsNone(channel.task);self.assertIsNone(channel.sender)
        finally:await self.app.close()

    @async_test
    async def test_actual_fake_model_final_is_delivered_once_in_bound_session(self):
        from bender.devtools.fake_llm import create_router
        await self.setup_matrix();model=HttpServer(create_router());await model.start('127.0.0.1',0)
        try:
            ctx,sid=await self.pair();ctx.settings.set('agent',{'awakening':False},by='owner')
            provider=self.app.models.add_provider(name='Фальшивая',kind='openai',base_url=f'http://127.0.0.1:{model.port}/v1',extra={'is_local':True})
            ctx.models.set_role('main',[{'provider':provider['id'],'model':'echo'}])
            self.app.matrix.stopping=False
            self.app.matrix.sender=asyncio.create_task(self.app.matrix.deliver())
            self.events=[self.event('Привет из Matrix',eid='$real-turn')];await self.app.matrix.poll_once()
            for _ in range(500):
                if len(self.messages)>1:break
                await asyncio.sleep(.01)
            self.assertEqual(len(self.messages),2)
            self.assertIn('Привет из Matrix',self.messages[-1]['content']['body'])
            self.assertEqual(ctx.store.session(sid)['meta']['home_channel'],'matrix')
            self.assertFalse(self.app.matrix.outbox.qsize())
        finally:await model.stop();await self.shutdown()


    @async_test
    async def test_tls_is_verified_and_download_redirect_not_followed(self):
        with mock.patch.dict(os.environ,{'SSL_CERT_FILE':str(FIXTURES/'mcp-oauth-cert.pem')}):
            await self.setup_matrix(tls=True)
            try:
                await self.pair()
                self.download_redirect=True
                with self.assertRaises(HumanError):await self.app.matrix.transport.call('download',{'uri':'mxc://example.org/valid_1'})
                self.assertEqual(sum(name=='download' for name,_ in self.calls),1)
                with mock.patch.dict(os.environ):
                    os.environ.pop('SSL_CERT_FILE',None)
                    with self.assertRaises(HumanError):await self.app.matrix.transport.call('status')
            finally:await self.shutdown()

    @async_test
    async def test_file_larger_than_default_mcp_frame_is_bounded_and_untrusted(self):
        await self.setup_matrix()
        try:
            ctx,sid=await self.pair();self.payload=b'x'*(2*1024*1024+1)
            self.events=[self.event('big.txt',eid='$big',content={'msgtype':'m.file','body':'big.txt','url':'mxc://example.org/big'})]
            with mock.patch.object(ctx.mind,'say',new=mock.AsyncMock()) as say:await self.app.matrix.poll_once()
            item=say.call_args.kwargs['message_meta']['attachments'][0]
            self.assertTrue(say.call_args.kwargs['tainted']);self.assertEqual(Path(item['path']).stat().st_size,len(self.payload))
            self.app.settings.set('matrix',{'max_file_mb':1},by='owner')
            with self.assertRaises(HumanError):await self.app.matrix.files(ToolContext(ctx,actor='owner',session_id=sid),[{'bytes':self.payload}],'')
            self.assertFalse(any(name=='upload' for name,_ in self.calls))
        finally:await self.shutdown()

    @async_test
    async def test_guard_wait_cannot_switch_recipient_or_reuse_old_pair_code(self):
        await self.setup_matrix()
        try:
            ctx,sid=await self.pair();initial=len(self.messages)
            original=self.app.guard.check
            async def change(action,**kw):
                result=await original(action,**kw)
                if action.capability=='msg.send':self.app.settings.set('matrix',{'room_id':'!other:example.org'},by='owner')
                return result
            with mock.patch.object(self.app.guard,'check',new=change):
                with self.assertRaises(HumanError):await self.app.matrix.send(ToolContext(ctx,actor='owner',session_id=sid),'НЕ ТОМУ ПОЛУЧАТЕЛЮ')
            self.assertEqual(len(self.messages),initial);self.assertFalse(self.app.matrix.owner())
        finally:await self.shutdown()


    @async_test
    async def test_owner_reaction_is_tainted_service_fact_without_model_turn(self):
        await self.setup_matrix()
        try:
            ctx,sid=await self.pair();number=self.app.matrix.remember(ctx,'$known',sid,'Сообщение','out')
            event=self.event('',eid='$reaction',kind='m.reaction',content={'m.relates_to':{'rel_type':'m.annotation','event_id':'$known','key':'👍'}})
            with mock.patch.object(ctx.mind,'say',new=mock.AsyncMock()) as say:await self.app.matrix.incoming(event,{'bot':BOT,'members':[BOT,OWNER]})
            self.assertEqual(say.await_count,0)
            row=ctx.store.one('SELECT meta,data FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1',(sid,))
            meta=json.loads(row['meta']);self.assertTrue(meta['untrusted']);self.assertTrue(meta['service_event']);self.assertEqual(meta['channel'],'matrix')
            self.assertIn(str(number),row['data'])
        finally:await self.shutdown()


    @async_test
    async def test_openclaw_proposals_are_private_public_fields_only_and_do_not_connect(self):
        from bender.modules.matrix_import import proposals
        await self.setup_matrix()
        try:
            self.app.store.meta_set('import:openclaw:channels',[{'matrix':{'homeserver':'https://matrix.example.org','accessToken':'DO_NOT_EXPORT','rooms':{ROOM:{}},'encryption':True,'accounts':{'second':{'homeserver':'https://second.example.org','password':'DO_NOT_EXPORT_EITHER'}}}}])
            before=len(self.calls);rows=proposals(self.app)
            self.assertEqual(len(rows),2);self.assertEqual(rows[0]['rooms'],[ROOM]);self.assertTrue(rows[0]['encrypted'])
            self.assertNotIn('DO_NOT_EXPORT',json.dumps(rows));self.assertEqual(len(self.calls),before)
            self.app.agents.create('Другой',agent_id='other',awaken=False)
            self.assertEqual(proposals(self.app.agents.get('other')),[])
        finally:await self.shutdown()
