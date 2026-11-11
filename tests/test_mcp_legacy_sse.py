"""Настоящий старый HTTP+SSE: два адреса, секреты, встречные запросы и обрыв."""
import asyncio
import json

from bender.core.app import App
from bender.core.errors import HumanError
from bender.mcp.client import Client
from bender.mcp.legacy_sse import LegacySse, endpoint_url
from bender.net.http_server import HttpServer, Router, Response, StreamResponse, sse
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test


class LegacySseTest(TempDirCase):
    async def fixture(self,*,endpoint='/messages?session=test',broken=False,burst=False,client_request=False):
        self.app=App(self.tmp/'data');self.queue=asyncio.Queue();self.posts=[];self.gets=[]
        self.calls=0;router=Router()
        @router.route('GET','/sse')
        async def incoming(req):
            self.gets.append(req.headers.get('authorization'))
            queue=asyncio.Queue();self.queue=queue
            async def stream():
                yield ('event: endpoint\ndata: '+endpoint+'\n\n').encode()
                if burst:
                    for i in range(40):yield sse({'jsonrpc':'2.0','method':'notifications/progress','params':{'message':str(i)}})
                    return
                while True:
                    value=await queue.get()
                    if value is None:return
                    yield sse(value)
            return StreamResponse(stream())
        @router.route('POST','/messages')
        async def outgoing(req):
            msg=req.json();self.posts.append((req.query,req.headers.get('authorization'),msg));method=msg.get('method')
            if not method:return Response(b'',202)
            if 'id' not in msg:return Response(b'',202)
            if method=='server/discover':reply={'error':{'code':-32601,'message':'Старый сервер'}}
            elif method=='initialize':reply={'result':{'protocolVersion':'2024-11-05','capabilities':{'tools':{},'resources':{}}}}
            elif method=='tools/list':
                await self.queue.put({'jsonrpc':'2.0','id':'server-request','method':'roots/list'})
                reply={'result':{'tools':[{'name':'echo','inputSchema':{'type':'object','properties':{'text':{'type':'string'}}}}]}}
            elif method=='tools/call':
                self.calls+=1
                if client_request:await self.queue.put({'jsonrpc':'2.0','id':'call-root','method':'roots/list'})
                if broken:await self.queue.put(None);return Response(b'',202)
                await self.queue.put({'jsonrpc':'2.0','method':'notifications/progress','params':{'message':'Идёт проверка'}})
                reply={'result':{'content':[{'type':'text','text':msg['params']['arguments']['text']}]}}
            elif method=='resources/list':reply={'result':{'resources':[{'uri':'notes:///a','name':'Заметка'}]}}
            elif method=='resources/templates/list':reply={'result':{'resourceTemplates':[]}}
            elif method=='resources/read':reply={'result':{'contents':[{'uri':'notes:///a','text':'Старая заметка'}]}}
            else:reply={'error':{'code':-32601,'message':'Нет метода'}}
            await self.queue.put({'jsonrpc':'2.0','id':msg['id'],**reply})
            return Response(b'',202)
        self.server=HttpServer(router);await self.server.start('127.0.0.1',0)
        self.url=f'http://127.0.0.1:{self.server.port}/sse'
        self.ctx=ToolContext(self.app,actor='owner',session_id='sse-test')

    async def finish(self):
        await self.app.close();await self.server.stop()

    @async_test
    async def test_guard_tools_resources_auth_and_refused_client_requests(self):
        await self.fixture()
        try:
            await self.app.mcp.save({'id':'old','transport':'sse','url':self.url,'enabled':True,'headers':{'Authorization':'Bearer sse-test-token'}})
            panel=await self.app.mcp.discover('old',self.ctx)
            self.app.mcp.approve('old',panel['servers'][0]['review'])
            self.app.guard.add_rule('mcp.call','old/*','allow')
            progress=[];self.ctx.progress=progress.append
            result=await self.app.mcp.call('old','echo',{'text':'Через SSE'},self.ctx)
            self.assertTrue(result.tainted);self.assertIn('Через SSE',result.content)
            self.assertEqual(progress,['Идёт проверка']);self.assertEqual(self.calls,1)
            self.app.settings.set('mcp',{'content_enabled':True},by='owner')
            panel=await self.app.mcp.catalog.discover('old',self.ctx)
            self.app.mcp.catalog.approve('old',panel['servers'][0]['content_review'])
            resource=await self.app.mcp.catalog.request('old','read',self.ctx,uri='notes:///a')
            self.assertIn('Старая заметка',resource.content)
            self.assertTrue(self.gets);self.assertTrue(all(auth=='Bearer sse-test-token' for auth in self.gets))
            self.assertTrue(all(auth=='Bearer sse-test-token' for _,auth,_ in self.posts))
            declined=[msg for _,_,msg in self.posts if msg.get('id')=='server-request']
            self.assertTrue(declined);self.assertTrue(all(msg['error']['code']==-32601 for msg in declined))
            self.assertNotIn('sse-test-token',json.dumps(self.app.settings.group('mcp')))
            client=self.app.mcp.clients['old'][1];await self.app.mcp.disconnect('old')
            self.assertTrue(client.transport.reader.done());self.assertEqual(client.transport.queued_bytes,0)
        finally:await self.finish()

    @async_test
    async def test_lost_response_does_not_repeat_action(self):
        await self.fixture(broken=True)
        try:
            await self.app.mcp.save({'id':'old','transport':'sse','url':self.url,'enabled':True})
            panel=await self.app.mcp.discover('old',self.ctx);self.app.mcp.approve('old',panel['servers'][0]['review'])
            self.app.guard.add_rule('mcp.call','old/*','allow')
            with self.assertRaises(HumanError):await self.app.mcp.call('old','echo',{'text':'одно действие'},self.ctx)
            self.assertEqual(self.calls,1);self.assertEqual(self.app.mcp.clients,{})
        finally:await self.finish()

    @async_test
    async def test_explicit_roots_during_call_on_old_sse(self):
        await self.fixture(client_request=True)
        try:
            def approve(event):
                if event.get('type')=='approval':asyncio.get_running_loop().call_soon(self.app.guard.respond,event['card']['id'],'once')
            self.app.bus.listen(approve);self.app.settings.set('mcp',{'client_enabled':True},by='owner')
            await self.app.mcp.save({'id':'old','transport':'sse','url':self.url,'enabled':True,'client_features':{'roots':True},'client_roots':[str(self.app.dirs.work)]})
            panel=await self.app.mcp.discover('old',self.ctx);self.app.mcp.approve('old',panel['servers'][0]['review'])
            result=await self.app.mcp.call('old','echo',{'text':'С правами roots'},self.ctx)
            self.assertTrue(result.ok)
            replies=[msg for _,_,msg in self.posts if msg.get('id')=='call-root']
            self.assertEqual(replies[0]['result']['roots'][0]['uri'],self.app.dirs.work.as_uri());self.assertEqual(self.calls,1)
        finally:await self.finish()

    @async_test
    async def test_foreign_endpoint_and_unsolicited_queue_are_bounded(self):
        await self.fixture(endpoint='https://other.invalid/messages')
        try:
            transport=LegacySse(self.app.http,self.url,{'Authorization':'не передавать'},2)
            client=Client(transport,timeout=2)
            try:
                with self.assertRaises(HumanError):await client.connect()
                self.assertEqual(self.posts,[])
            finally:await client.close()
        finally:await self.finish()
        await self.fixture(burst=True)
        try:
            transport=LegacySse(self.app.http,self.url,{},2);client=Client(transport,timeout=2)
            try:
                with self.assertRaises(HumanError):await client.connect()
                self.assertLessEqual(transport.queued_bytes,2*1024*1024)
            finally:await client.close()
            self.assertEqual(transport.queue.qsize(),0)
        finally:await self.finish()

    @async_test
    async def test_import_disabled_and_origin_canonicalization(self):
        app=App(self.tmp/'data')
        try:
            result=app.mcp.import_servers({'mcpServers':{'Старый':{'type':'sse','url':'https://example.org/sse'}}})
            self.assertEqual(len(result['imported']),1);self.assertFalse(result['servers'][0]['enabled'])
            self.assertEqual(result['servers'][0]['transport'],'sse');self.assertEqual(app.mcp.clients,{})
            self.assertEqual(endpoint_url('https://EXAMPLE.org/sse','https://example.org:443/messages'),'https://example.org:443/messages')
            for value in ('http://example.org/messages','//other.org/messages','https://user:pass@example.org/messages','/messages#fragment','/bad\npath','\\other.org/messages'):
                with self.assertRaises(HumanError):endpoint_url('https://example.org/sse',value)
        finally:await app.close()
