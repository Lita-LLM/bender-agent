"""Реальные stdio/HTTP Tasks и подписки: полномочия, события, ввод и отсутствие повторов."""
import asyncio
import copy
import json
from unittest import mock

from bender.core.app import App
from bender.core.errors import HumanError
from bender.mcp.client import Client,NEW
from bender.mcp.requests import broker
from bender.mcp.tasks import EXTENSION,state
from bender.net.http_server import HttpServer,Router,JSONResponse,StreamResponse,Response,sse
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test,owner_web,owner_headers
from tests import test_mcp_client as base
from tests.test_mcp_requests import FORM

TASK={'resultType':'task','taskId':'opaque-task-Ж','status':'working','createdAt':'2026-10-26T00:00:00Z','lastUpdatedAt':'2026-10-26T00:00:01Z','ttlMs':600000,'pollIntervalMs':5000}


class McpBackgroundTest(TempDirCase):
    def setUp(self):
        super().setUp();self.app=App(self.tmp/'data');folder=self.tmp/'server';folder.mkdir()
        self.state=folder/'mcp-state.json';self.state.write_text('{}')
        self.app.settings.set('mcp',{'tasks_enabled':True,'subscriptions_enabled':True,'client_enabled':True},by='owner')
        self.app.guard.add_rule('mcp.call','local/*','allow',only_clean=False)
        self.app.guard.add_rule('mcp.connect','local/*','allow',only_clean=False)
        self.approvals=[]
        def answer(event):
            if event.get('type')=='approval':
                self.approvals.append(event['card'])
                asyncio.get_running_loop().call_soon(self.app.guard.respond,event['card']['id'],'once')
        self.app.bus.listen(answer)
    context=base.McpClientTest.context
    attach=base.McpClientTest.attach
    change=base.McpClientTest.change

    async def ready(self,**extra):
        self.change(task=TASK,capabilities={'tools':{},'extensions':{EXTENSION:{}}})
        review=await self.attach(tasks_enabled=True,client_features={'elicitation':True},**extra)
        self.app.mcp.approve('local',review)

    async def create(self,**kw):
        result=await self.app.mcp.call('local','echo',{'text':'работа'},self.context(**kw))
        self.assertTrue(result.tainted);return next(reversed(self.app.mcp.tasks.records().values()))

    @async_test
    async def test_stdio_creation_persistence_status_cancel_without_tool_replay(self):
        try:
            await self.ready();entry=await self.create(actor='agent')
            wire=json.loads(self.state.read_text());self.assertEqual(wire['calls'],1)
            self.assertIn(EXTENSION,wire['task_metadata']['io.modelcontextprotocol/clientCapabilities']['extensions'])
            self.assertEqual(len(self.app.mcp.tasks.public(self.context(actor='agent'))),1)
            await self.app.mcp.tasks.request('cancel',entry['id'],self.context(actor='agent'))
            record=self.app.mcp.tasks.records()[entry['id']];self.assertTrue(record['cancel_requested']);self.assertEqual(record['state']['status'],'working')
            self.change(task_state={**TASK,'resultType':'complete','status':'completed','result':{'content':[{'type':'text','text':'ГОТОВО'}]}})
            await self.app.mcp.close();self.app._mcp=None
            result=await self.app.mcp.tasks.request('status',entry['id'],self.context(actor='agent'))
            self.assertIn('ГОТОВО',result.content)
            wire=json.loads(self.state.read_text());self.assertEqual(wire['calls'],1)
            self.assertEqual([r['method'] for r in wire['task_methods']],['tasks/cancel','tasks/get'])
            self.assertFalse(any(t.get_name().startswith('mcp-task') for t in asyncio.all_tasks()))
        finally:await self.app.close()

    @async_test
    async def test_disabled_and_legacy_do_not_accept_or_replay_task(self):
        try:
            await self.ready();self.app.settings.set('mcp',{'tasks_enabled':False})
            with self.assertRaises(HumanError):await self.create(actor='owner')
            self.assertFalse(self.app.mcp.tasks.records());self.assertEqual(json.loads(self.state.read_text())['calls'],1)
            self.app.settings.set('mcp',{'tasks_enabled':True});self.change(legacy=True)
            review=await self.attach(tasks_enabled=True);self.app.mcp.approve('local',review)
            with self.assertRaises(HumanError):await self.create(actor='owner')
            self.assertEqual(json.loads(self.state.read_text())['calls'],2)
        finally:await self.app.close()

    @async_test
    async def test_session_role_definition_and_secret_rotation_are_rechecked(self):
        try:
            await self.ready(env={'TASK_SECRET':'НЕ-ОТПРАВЛЯТЬ'});entry=await self.create(actor='agent')
            other=ToolContext(self.app,actor='agent',session_id='other')
            self.assertEqual(self.app.mcp.tasks.public(other),[])
            with self.assertRaises(HumanError):await self.app.mcp.tasks.request('status',entry['id'],other)
            with mock.patch.object(self.app.team,'permissions',return_value={'capabilities':[]}):
                with self.assertRaises(HumanError):await self.app.mcp.tasks.request('status',entry['id'],self.context(actor='agent'))
            self.change(description='ПОДМЕНА')
            with self.assertRaises(HumanError):await self.app.mcp.tasks.request('cancel',entry['id'],self.context(actor='owner'))
            self.assertNotIn('task_methods',json.loads(self.state.read_text()))
            self.change(description='Повторить аргументы')
            for name in self.app.secrets.names():
                if self.app.secrets.get(name)=='НЕ-ОТПРАВЛЯТЬ':self.app.secrets.set(name,'ДРУГОЙ',allow=self.app.secrets.allow(name))
            with self.assertRaises(HumanError):await self.app.mcp.tasks.request('status',entry['id'],self.context(actor='owner'))
        finally:await self.app.close()

    @async_test
    async def test_task_input_uses_tainted_guard_owner_form_and_not_twice(self):
        try:
            await self.ready();entry=await self.create(actor='owner')
            self.change(task_state={**TASK,'resultType':'complete','status':'input_required','inputRequests':{'f':{'method':'elicitation/create','params':FORM}}})
            waiting=asyncio.create_task(self.app.mcp.tasks.request('input',entry['id'],self.context(actor='owner')))
            for _ in range(200):
                cards=broker(self.app).public()
                if cards:break
                await asyncio.sleep(.01)
            self.assertTrue(cards);broker(self.app).answer(cards[0]['id'],'accept',{'тема':'Ввод','число':2});await waiting
            self.assertTrue(any(c['tainted'] and '/client/elicitation' in c['target'] for c in self.approvals))
            with self.assertRaises(HumanError):await self.app.mcp.tasks.request('input',entry['id'],self.context(actor='owner'))
            wire=json.loads(self.state.read_text());self.assertEqual(wire['calls'],1)
            self.assertEqual(sum(r['method']=='tasks/update' for r in wire['task_methods']),1)
            self.assertEqual(wire['task_input']['f']['content']['тема'],'Ввод')
            self.assertNotIn('Ввод',json.dumps(self.app.mcp.tasks.records(),ensure_ascii=False))
        finally:await self.app.close()

    @async_test
    async def test_namespace_separation_and_capacity_before_side_effect(self):
        try:
            await self.ready();entry=await self.create(actor='owner')
            other=self.app.agents.create('Другой',agent_id='other');ctx=self.app.agents.get(other['id'])
            self.assertEqual(ctx.mcp.tasks.records(),{})
            with self.assertRaises(HumanError):await ctx.mcp.tasks.request('status',entry['id'],ToolContext(ctx,actor='owner'))
            self.app.store.meta_set('mcp:tasks',{str(i):{**entry,'id':str(i)} for i in range(100)})
            with self.assertRaises(HumanError):await self.create(actor='owner')
            self.assertEqual(json.loads(self.state.read_text())['calls'],1)
            await self.app.mcp.tasks.request('forget','0',self.context(actor='owner'));self.assertEqual(len(self.app.mcp.tasks.records()),99)
        finally:await self.app.close()

    def test_state_validation(self):
        try:
            for extra in ({'taskId':''},{'ttlMs':True},{'status':'unknown'},{'pollIntervalMs':-1},{'inputRequests':{'x':[]}}, {'status':'completed'}, {'result':{'x':float('nan')}}):
                with self.assertRaises(HumanError):state({**TASK,**extra})
        finally:asyncio.run(self.app.close())

    async def http_ready(self,*,wrong_ack=False):
        self.messages=[];self.events=asyncio.Queue();self.stream_closed=asyncio.Event();self.opened=asyncio.Event()
        router=Router()
        @router.route('POST','/mcp')
        async def serve(req):
            msg=req.json();self.messages.append((msg,{key.lower():value for key,value in req.headers.items}))
            method=msg['method'];result={}
            if method=='server/discover':result={'supportedVersions':[NEW],'capabilities':{'tools':{},'resources':{},'prompts':{},'extensions':{EXTENSION:{}}}}
            elif method=='tools/list':result={'tools':[{'name':'echo','inputSchema':{'type':'object'}}]}
            elif method=='tools/call':
                if hasattr(self,'block_calls'):
                    self.block_calls[0].set();await self.block_calls[1].wait()
                result=copy.deepcopy(TASK)
            elif method=='tasks/get':result={**TASK,'resultType':'complete','status':'completed','result':{'content':[{'type':'text','text':'По HTTP'}]}}
            elif method=='tasks/cancel':result={'resultType':'complete'}
            elif method=='resources/list':result={'resources':[{'name':'Ресурс','uri':'notes:///test'}]}
            elif method=='resources/templates/list':result={'resourceTemplates':[]}
            elif method=='prompts/list':result={'prompts':[]}
            elif method=='subscriptions/listen':
                async def generate():
                    try:
                        meta={'io.modelcontextprotocol/subscriptionId':'wrong' if wrong_ack else msg['id']}
                        yield sse({'jsonrpc':'2.0','method':'notifications/subscriptions/acknowledged','params':{'_meta':meta,'notifications':msg['params']['notifications']}})
                        self.opened.set()
                        while True:
                            event=await self.events.get()
                            if event=='close':
                                yield sse({'jsonrpc':'2.0','id':msg['id'],'result':{'resultType':'complete','_meta':meta}});return
                            yield sse({'jsonrpc':'2.0','method':event['method'],'params':{'_meta':meta,**event.get('params',{})}})
                    finally:self.stream_closed.set()
                return StreamResponse(generate(),content_type='text/event-stream')
            return JSONResponse({'jsonrpc':'2.0','id':msg['id'],'result':result})
        server=HttpServer(router);await server.start('127.0.0.1',0)
        await self.app.mcp.save({'id':'local','transport':'http','url':f'http://127.0.0.1:{server.port}/mcp','enabled':True,'tasks_enabled':True})
        await self.app.mcp.discover('local',self.context(actor='owner'));self.app.mcp.approve('local',self.app.mcp.public()['servers'][0]['review'])
        return server

    async def event_wait(self,predicate):
        for _ in range(200):
            if predicate():return
            await asyncio.sleep(.01)
        self.fail('Не пришло ожидаемое событие')

    @async_test
    async def test_http_task_headers_and_no_get_result(self):
        server=await self.http_ready()
        try:
            entry=await self.create(actor='owner');result=await self.app.mcp.tasks.request('status',entry['id'],self.context(actor='owner'))
            self.assertIn('По HTTP',result.content)
            message,headers=next(item for item in self.messages if item[0]['method']=='tasks/get')
            self.assertEqual(headers['mcp-name'],'=?base64?b3BhcXVlLXRhc2st0JY=?=')
            self.assertIn(EXTENSION,message['params']['_meta']['io.modelcontextprotocol/clientCapabilities']['extensions'])
            self.assertFalse(any(msg['method']=='tasks/result' for msg,_ in self.messages))
        finally:await self.app.close();await server.stop()

    @async_test
    async def test_subscription_ack_notifications_quarantine_and_graceful_close(self):
        server=await self.http_ready()
        try:
            self.app.settings.set('mcp',{'content_enabled':True},by='owner')
            await self.app.mcp.catalog.discover('local',self.context(actor='owner'))
            self.app.mcp.catalog.approve('local',self.app.mcp.public()['servers'][0]['content_review'])
            await self.app.mcp.subscriptions.start('local',{'toolsListChanged':True,'resourceSubscriptions':['notes:///test']},self.context(actor='owner'))
            await self.event_wait(lambda:self.app.mcp.subscriptions.states['local']['acknowledged'])
            self.events.put_nowait({'method':'notifications/resources/updated','params':{'uri':'notes:///test'}})
            await self.event_wait(lambda:'resources/updated' in self.app.mcp.subscriptions.states['local']['message'])
            self.assertFalse(any(msg['method']=='resources/read' for msg,_ in self.messages))
            self.events.put_nowait({'method':'notifications/tools/list_changed'})
            await self.event_wait(lambda:self.app.mcp.definition('local','echo') is None)
            self.events.put_nowait('close');await self.event_wait(lambda:not self.app.mcp.subscriptions.active)
            self.assertEqual(self.app.mcp.subscriptions.states['local']['message'],'Сервер завершил подписку')
            self.assertEqual(sum(msg['method']=='subscriptions/listen' for msg,_ in self.messages),1)
        finally:await self.app.close();await server.stop()

    @async_test
    async def test_subscription_refuses_unapproved_uri_unknown_task_and_wrong_id(self):
        server=await self.http_ready(wrong_ack=True)
        try:
            for filters in ({'resourceSubscriptions':['file:///private']},{'taskIds':['stranger']},{'unknown':True}):
                with self.assertRaises(HumanError):await self.app.mcp.subscriptions.start('local',filters,self.context(actor='owner'))
            await self.app.mcp.subscriptions.start('local',{'toolsListChanged':True},self.context(actor='owner'))
            await self.event_wait(lambda:not self.app.mcp.subscriptions.active)
            self.assertIn('другой подписке',self.app.mcp.subscriptions.states['local']['message'])
            self.assertFalse(self.app.mcp.subscriptions.states['local']['acknowledged'])
        finally:await self.app.close();await server.stop()

    @async_test
    async def test_task_notification_only_known_and_shutdown_no_retry(self):
        server=await self.http_ready()
        try:
            entry=await self.create(actor='owner')
            await self.app.mcp.subscriptions.start('local',{'taskIds':[entry['id']]},self.context(actor='owner'))
            await self.event_wait(lambda:self.app.mcp.subscriptions.states['local']['acknowledged'])
            self.events.put_nowait({'method':'notifications/tasks','params':{**TASK,'status':'completed','result':{'content':[]}}})
            await self.event_wait(lambda:self.app.mcp.tasks.records()[entry['id']]['state']['status']=='completed')
            self.app.settings.set('mcp',{'subscriptions_enabled':False})
            await self.event_wait(lambda:not self.app.mcp.subscriptions.active)
            self.assertEqual(sum(msg['method']=='subscriptions/listen' for msg,_ in self.messages),1)
            self.assertEqual(sum(msg['method']=='tools/call' for msg,_ in self.messages),1)
        finally:await self.app.close();await server.stop()

    @async_test
    async def test_owner_api_authentication_and_forget_without_server_action(self):
        await self.ready();entry=await self.create(actor='owner')
        web=HttpServer(owner_web(self.app,allowed_hosts={'127.0.0.1','localhost'}));await web.start('127.0.0.1',0)
        url=f'http://127.0.0.1:{web.port}/api/mcp/tasks/{entry["id"]}/forget'
        try:
            response=await self.app.http.post(url,json={},headers={'X-Bender':'1'})
            async with response:self.assertEqual(response.status,401);await response.read()
            self.assertIn(entry['id'],self.app.mcp.tasks.records())
            response=await self.app.http.post(url,json={},headers=owner_headers(**{'X-Bender':'1'}))
            async with response:self.assertEqual(response.status,200);await response.read()
            self.assertNotIn(entry['id'],self.app.mcp.tasks.records())
            self.assertEqual(json.loads(self.state.read_text())['calls'],1)
        finally:await web.stop();await self.app.close()

    @async_test
    async def test_task_input_revoked_during_form_never_sends_update(self):
        try:
            await self.ready();entry=await self.create(actor='owner')
            self.change(task_state={**TASK,'resultType':'complete','status':'input_required','inputRequests':{'f':{'method':'elicitation/create','params':FORM}}})
            waiting=asyncio.create_task(self.app.mcp.tasks.request('input',entry['id'],self.context(actor='owner')))
            await self.event_wait(lambda:bool(broker(self.app).public()))
            trust=self.app.settings.group('mcp_trust');trust['local']['tools']={};self.app.settings.replace('mcp_trust',trust)
            card=broker(self.app).public()[0];broker(self.app).answer(card['id'],'accept',{'тема':'Ввод','число':2})
            with self.assertRaises(HumanError):await waiting
            wire=json.loads(self.state.read_text());self.assertFalse(any(r['method']=='tasks/update' for r in wire['task_methods']))
        finally:await self.app.close()

    @async_test
    async def test_subscription_flood_stops_without_auto_retry(self):
        server=await self.http_ready()
        try:
            await self.app.mcp.subscriptions.start('local',{'toolsListChanged':True},self.context(actor='owner'))
            await self.event_wait(lambda:self.app.mcp.subscriptions.states['local']['acknowledged'])
            for _ in range(121):self.events.put_nowait({'method':'notifications/tools/list_changed'})
            await self.event_wait(lambda:not self.app.mcp.subscriptions.active)
            self.assertIn('120 событий',self.app.mcp.subscriptions.states['local']['message'])
            self.assertEqual(sum(msg['method']=='subscriptions/listen' for msg,_ in self.messages),1)
        finally:await self.app.close();await server.stop()

    @async_test
    async def test_subscription_checks_catalog_revocation_after_guard_await(self):
        server=await self.http_ready()
        try:
            self.app.settings.set('mcp',{'content_enabled':True},by='owner')
            await self.app.mcp.catalog.discover('local',self.context(actor='owner'));self.app.mcp.catalog.approve('local',self.app.mcp.public()['servers'][0]['content_review'])
            # Отзыв после реального решения Guard, до отправки подписки.
            with mock.patch.object(self.app.guard,'check',wraps=self.app.guard.check) as check:
                async def after(*args,**kw):
                    result=await check._mock_wraps(*args,**kw)
                    if args[0].capability=='mcp.call':
                        trust=self.app.settings.group('mcp_trust');trust['local']['content']={};self.app.settings.replace('mcp_trust',trust)
                    return result
                check.side_effect=after
                with self.assertRaises(HumanError):await self.app.mcp.subscriptions.start('local',{'resourceSubscriptions':['notes:///test']},self.context(actor='owner'))
            self.assertFalse(any(msg['method']=='subscriptions/listen' for msg,_ in self.messages))
        finally:await self.app.close();await server.stop()


    @async_test
    async def test_concurrent_servers_reserve_capacity_before_call(self):
        server=await self.http_ready()
        try:
            entry=await self.create(actor='owner')
            self.app.store.meta_set('mcp:tasks',{str(i):{**entry,'id':str(i)} for i in range(99)})
            await self.app.mcp.save({**self.app.mcp.get('local'),'id':'second'})
            await self.app.mcp.discover('second',self.context(actor='owner'))
            self.app.mcp.approve('second',next(row['review'] for row in self.app.mcp.public()['servers'] if row['id']=='second'))
            self.app.guard.add_rule('mcp.call','second/*','allow');self.app.guard.add_rule('mcp.connect','second/*','allow')
            self.block_calls=(asyncio.Event(),asyncio.Event())
            first=asyncio.create_task(self.app.mcp.call('local','echo',{},self.context(actor='owner')))
            await self.block_calls[0].wait()
            with self.assertRaises(HumanError):await self.app.mcp.call('second','echo',{},self.context(actor='owner'))
            self.block_calls[1].set();await first
            self.assertEqual(len(self.app.mcp.tasks.records()),100)
            self.assertEqual(sum(msg['method']=='tools/call' for msg,_ in self.messages),2)
        finally:await self.app.close();await server.stop()
