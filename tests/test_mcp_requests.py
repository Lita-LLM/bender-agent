"""Встречные права MCP, настоящие stdio/MRTR, формы хозяина и изоляция sampling."""
import asyncio
import json
from unittest import mock

from bender.core.app import App
from bender.core.errors import HumanError
from bender.core.settings import NeedsOwner
from bender.mcp import forms
from bender.mcp.client import Client
from bender.mcp.requests import Requests, broker
from bender.net.http_server import HttpServer,Router,JSONResponse,Response,StreamResponse,sse
from bender.router.types import Result,Usage
from tests.helpers import TempDirCase,async_test,owner_headers,owner_web
from tests import test_mcp_client as base

FORM={'mode':'form','message':'Уточни данные','requestedSchema':{'type':'object','properties':{'тема':{'type':'string','minLength':2},'число':{'type':'integer','minimum':1,'maximum':5}},'required':['тема','число']}}


class McpRequestsTest(TempDirCase):
    def setUp(self):
        super().setUp();self.app=App(self.tmp/'data');folder=self.tmp/'server';folder.mkdir()
        self.state=folder/'mcp-state.json';self.state.write_text('{}')
        self.app.settings.set('mcp',{'client_enabled':True},by='owner')
        self.app.guard.add_rule('mcp.call','local/*','allow',only_clean=False)
        self.app.guard.add_rule('mcp.connect','local/*','allow',only_clean=False)
        self.approvals=[];self.approval_delay=0
        def answer(event):
            if event.get('type')=='approval':
                self.approvals.append(event['card'])
                asyncio.get_running_loop().call_later(self.approval_delay,self.app.guard.respond,event['card']['id'],'once')
        self.app.bus.listen(answer)
    change=base.McpClientTest.change
    context=base.McpClientTest.context
    attach=base.McpClientTest.attach

    async def ready(self,**kw):
        review=await self.attach(client_features={'roots':True,'sampling':True,'elicitation':True},client_roots=[str(self.app.dirs.work)],**kw)
        self.app.mcp.approve('local',review)

    async def call(self,**kw):return await self.app.mcp.call('local','echo',{'text':'вызов'},self.context(**kw))

    async def pending(self):
        for _ in range(200):
            cards=broker(self.app).public()
            if cards:return cards[0]
            await asyncio.sleep(.01)
        self.fail('Форма не появилась')

    @async_test
    async def test_mrtr_roots_are_names_only_and_one_action(self):
        try:
            self.change(client_requests={'r':{'method':'roots/list','params':{}}})
            (self.app.dirs.work/'private.txt').write_text('ЛИЧНЫЙ-ТЕКСТ')
            await self.ready();result=await self.call()
            state=json.loads(self.state.read_text());roots=state['client_responses']['r']['roots']
            self.assertEqual(roots,[{'uri':self.app.dirs.work.as_uri(),'name':self.app.dirs.work.name}])
            self.assertNotIn('ЛИЧНЫЙ-ТЕКСТ',result.content)
            self.assertEqual(state['calls'],1);self.assertEqual(state['round_trips'],2)
            self.assertTrue(any(card['tainted'] and '/client/roots/list' in card['target'] for card in self.approvals))
            self.assertEqual(set(state['client_metadata']['io.modelcontextprotocol/clientCapabilities']),{'roots','sampling','elicitation'})
        finally:await self.app.close()

    @async_test
    async def test_disabled_no_mrtr_replay_and_legacy_method_not_found(self):
        try:
            self.app.settings.set('mcp',{'client_enabled':False})
            self.change(client_requests={'r':{'method':'roots/list'}})
            await self.ready()
            with self.assertRaises(HumanError):await self.call()
            state=json.loads(self.state.read_text());self.assertNotIn('calls',state);self.assertEqual(state['round_trips'],1)
            self.change(legacy=True);await self.ready();await self.call()
            state=json.loads(self.state.read_text());self.assertEqual(state['client_responses']['r']['error']['code'],-32601)
            with self.assertRaises(NeedsOwner):self.app.settings.set('mcp',{'client_enabled':True},by='agent')
        finally:await self.app.close()

    @async_test
    async def test_cached_owner_connection_does_not_give_subagent_file_rights(self):
        try:
            self.change(legacy=True,client_requests={'r':{'method':'roots/list'}});await self.ready()
            with mock.patch.object(self.app.team,'permissions',return_value={'capabilities':['mcp.call','mcp.connect']}):
                await self.call(actor='subagent:test')
            response=json.loads(self.state.read_text())['client_responses']['r']
            self.assertIn('error',response);self.assertNotIn(self.app.dirs.work.as_uri(),json.dumps(response))
        finally:await self.app.close()

    @async_test
    async def test_sampling_no_tools_history_system_preferences_or_main_fallback(self):
        try:
            params={'messages':[{'role':'user','content':{'type':'text','text':'ВНЕШНИЙ-ВОПРОС'}}],
                    'maxTokens':9000,'systemPrompt':'ОПАСНЫЙ-СИСТЕМНЫЙ','modelPreferences':{'hints':[{'name':'main'}]}}
            self.change(client_requests={'s':{'method':'sampling/createMessage','params':params}})
            await self.ready();self.app.store.add_message(self.app.main_session(),{'role':'user','content':'ЛИЧНАЯ-ИСТОРИЯ'})
            result=Result(text='Короткий ответ',model='fake-fast',provider_name='фальшивый',usage=Usage(input=2,output=3),cost=.01)
            with mock.patch.object(self.app.router,'call',new=mock.AsyncMock(return_value=result)) as call:
                await self.call()
            args,kw=call.call_args;wire=json.dumps(args,ensure_ascii=False)
            self.assertNotIn('ЛИЧНАЯ-ИСТОРИЯ',wire);self.assertNotIn('ОПАСНЫЙ-СИСТЕМНЫЙ',wire)
            self.assertIn('ВНЕШНИЙ-ВОПРОС',wire);self.assertIn('НЕДОВЕРЕННОЕ',wire)
            self.assertEqual(kw['role'],'fast');self.assertIsNone(kw['tools']);self.assertFalse(kw['fallback_main']);self.assertTrue(kw['single_attempt']);self.assertEqual(kw['params']['max_tokens'],1024)
            self.assertEqual(json.loads(self.state.read_text())['client_responses']['s']['model'],'fake-fast')
            self.assertEqual(self.app.store.one('SELECT COUNT(*) AS n FROM calls')['n'],1)
        finally:await self.app.close()

    @async_test
    async def test_sampling_rejects_context_tools_and_secrets_in_output(self):
        try:
            await self.ready();handler=self.app.mcp.input_handler(self.app.mcp.get('local'),self.context())
            params={'messages':[{'role':'user','content':{'type':'text','text':'данные'}}],'maxTokens':2}
            for bad in ({'method':[]},{'method':'sampling/createMessage','params':{'metadata':float('inf')}}):
                with self.assertRaises(HumanError):await handler(bad)
            for extra in ({'includeContext':'allServers'},{'tools':[]},{'messages':[{'role':'system','content':{'type':'text','text':'не надо'}}]}):
                with self.assertRaises(HumanError):await handler({'method':'sampling/createMessage','params':{**params,**extra}})
            self.app.secrets.set('sample-secret','ЗНАЧЕНИЕ-ИЗ-СЕЙФА')
            with mock.patch.object(self.app.router,'call',new=mock.AsyncMock(return_value=Result(text='ЗНАЧЕНИЕ-ИЗ-СЕЙФА',model='fake'))) as call:
                result=await self.app.mcp.input_handler(self.app.mcp.get('local'),self.context())({'method':'sampling/createMessage','params':params})
                self.assertNotIn('ЗНАЧЕНИЕ-ИЗ-СЕЙФА',result['content']['text']);self.assertEqual(call.await_count,1)
        finally:await self.app.close()

    @async_test
    async def test_sampling_actual_fake_model_http_records_cost_and_no_tools(self):
        from bender.devtools.fake_llm import create_router
        fake=create_router();server=HttpServer(fake);await server.start('127.0.0.1',0)
        try:
            provider=self.app.models.add_provider(name='Локальный фейк',kind='openai',base_url=f'http://127.0.0.1:{server.port}/v1',extra={'is_local':True})
            self.app.models.set_role('fast',[{'provider':provider['id'],'model':'echo'}])
            self.change(client_requests={'s':{'method':'sampling/createMessage','params':{'messages':[{'role':'user','content':{'type':'text','text':'Текст для sampling'}}],'maxTokens':12}}})
            await self.ready();await self.call()
            response=json.loads(self.state.read_text())['client_responses']['s']
            self.assertIn('Эхо:',response['content']['text']);self.assertEqual(response['model'],'echo')
            self.assertEqual(len(fake.state.requests),1)
            request=fake.state.requests[0]['body'];self.assertFalse(request.get('tools'));self.assertEqual(request.get('max_tokens'),12)
            self.assertEqual(self.app.store.one('SELECT COUNT(*) AS n FROM calls')['n'],1)
        finally:await server.stop();await self.app.close()

    @async_test
    async def test_legacy_streamable_http_server_request_gets_post_response(self):
        router=Router();answer=asyncio.Event();received=[];calls=[]
        @router.route('POST','/mcp')
        async def reply(req):
            msg=req.json();method=msg.get('method')
            if not method:received.append(msg);answer.set();return Response(b'',202)
            if method=='server/discover':return JSONResponse({'error':{'code':-32601}},400)
            if method=='initialize':result={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
            elif method=='notifications/initialized':return Response(b'',202)
            elif method=='tools/list':result={'tools':[{'name':'echo','inputSchema':{'type':'object'}}]}
            else:
                calls.append(msg)
                async def stream():
                    yield sse({'jsonrpc':'2.0','id':'server-root','method':'roots/list'})
                    await answer.wait()
                    yield sse({'jsonrpc':'2.0','id':msg['id'],'result':{'content':[{'type':'text','text':'HTTP с ответом клиента'}]}})
                return StreamResponse(stream())
            return JSONResponse({'jsonrpc':'2.0','id':msg['id'],'result':result})
        server=HttpServer(router);await server.start('127.0.0.1',0)
        try:
            await self.app.mcp.save({'id':'local','transport':'http','url':f'http://127.0.0.1:{server.port}/mcp','enabled':True,'client_features':{'roots':True},'client_roots':[str(self.app.dirs.work)]})
            panel=await self.app.mcp.discover('local',self.context(actor='owner'));self.app.mcp.approve('local',panel['servers'][0]['review'])
            self.app.mcp.clients['local'][1].transport.timeout=.1;self.approval_delay=.2
            result=await self.call();self.assertIn('HTTP с ответом клиента',result.content)
            self.assertEqual(len(calls),1);self.assertEqual(received[0]['result']['roots'][0]['uri'],self.app.dirs.work.as_uri())
        finally:await server.stop();await self.app.close()

    @async_test
    async def test_form_owner_api_validation_and_mrtr_continuation(self):
        web=HttpServer(owner_web(self.app,allowed_hosts={'127.0.0.1','localhost'}));await web.start('127.0.0.1',0)
        task=None
        try:
            self.change(client_requests={'f':{'method':'elicitation/create','params':FORM}});await self.ready()
            task=asyncio.create_task(self.call());card=await self.pending()
            url=f'http://127.0.0.1:{web.port}/api/mcp/inputs/'+card['id']
            response=await self.app.http.post(url,json={'action':'accept','content':{'тема':'Проверка','число':2}},headers={'X-Bender':'1'})
            self.assertEqual(response.status,401);await response.read();self.assertFalse(task.done())
            with self.assertRaises(HumanError):broker(self.app).answer(card['id'],'accept',{'тема':'x','число':99})
            self.assertFalse(task.done())
            response=await self.app.http.post(url,json={'action':'accept','content':{'тема':'Проверка','число':2}},headers=owner_headers(**{'X-Bender':'1'}))
            self.assertTrue(response.ok,await response.text());await task
            state=json.loads(self.state.read_text());self.assertEqual(state['client_responses']['f'],{'action':'accept','content':{'тема':'Проверка','число':2}})
            self.assertEqual(state['calls'],1);self.assertEqual(broker(self.app).public(),[])
            with self.assertRaises(HumanError):broker(self.app).answer(card['id'],'accept',{})
        finally:
            if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            await web.stop();await self.app.close()

    @async_test
    async def test_form_revocation_and_cancel_do_not_leak_reply(self):
        task=None
        try:
            self.change(client_requests={'f':{'method':'elicitation/create','params':FORM}});await self.ready()
            task=asyncio.create_task(self.call());card=await self.pending()
            trust=self.app.settings.group('mcp_trust');trust['local'].pop('tools');self.app.settings.replace('mcp_trust',trust)
            broker(self.app).answer(card['id'],'accept',{'тема':'Не отправлять','число':2})
            with self.assertRaises(HumanError):await task
            self.assertNotIn('calls',json.loads(self.state.read_text()))
            await self.ready();task=asyncio.create_task(self.call());await self.pending()
            self.app.settings.set('mcp',{'client_enabled':False})
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(broker(self.app).public(),[])
        finally:
            if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            await self.app.close()

    @async_test
    async def test_url_unattended_and_secrets_never_auto_fill(self):
        task=None
        try:
            await self.ready();handler=self.app.mcp.input_handler(self.app.mcp.get('local'),self.context())
            for url in ('javascript:alert(1)','http://example.org','https://u:p@example.org'):
                with self.assertRaises(HumanError):await handler({'method':'elicitation/create','params':{'mode':'url','message':'Вход','url':url}})
            handler=self.app.mcp.input_handler(self.app.mcp.get('local'),self.context())
            task=asyncio.create_task(handler({'method':'elicitation/create','params':{'mode':'url','message':'Вход','url':'https://example.org/login'}}))
            card=await self.pending();broker(self.app).answer(card['id'],'accept')
            self.assertEqual(await task,{'action':'accept'})
            handler=self.app.mcp.input_handler(self.app.mcp.get('local'),self.context(unattended=True))
            self.assertEqual(await handler({'method':'elicitation/create','params':FORM}),{'action':'cancel'})
            self.app.secrets.set('form-secret','СЕКРЕТ-НЕ-ОТПРАВЛЯТЬ')
            task=asyncio.create_task(self.app.mcp.input_handler(self.app.mcp.get('local'),self.context())({'method':'elicitation/create','params':FORM}))
            card=await self.pending()
            with self.assertRaises(HumanError):broker(self.app).answer(card['id'],'accept',{'тема':'СЕКРЕТ-НЕ-ОТПРАВЛЯТЬ','число':2})
            broker(self.app).answer(card['id'],'decline');self.assertEqual(await task,{'action':'decline'})
        finally:
            if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            await self.app.close()

    @async_test
    async def test_loop_and_per_turn_limit_stop_without_an_action(self):
        try:
            self.change(loop_input=True,client_requests={'r':{'method':'roots/list'}});await self.ready()
            with self.assertRaises(HumanError):await self.call()
            state=json.loads(self.state.read_text());self.assertEqual(state['round_trips'],4);self.assertNotIn('calls',state)
            self.app.settings.set('mcp',{'client_requests':1});await self.ready()
            self.change(loop_input=False,client_requests={'a':{'method':'roots/list'},'b':{'method':'roots/list'}})
            with self.assertRaises(HumanError):await self.call()
            self.assertNotIn('calls',json.loads(self.state.read_text()))
        finally:await self.app.close()

    def test_schema_and_answer_types_are_checked(self):
        spec={'type':'object','properties':{'s':{'type':'string','oneOf':[{'const':'а','title':'А'},{'const':'б','title':'Б'}]},'multi':{'type':'array','items':{'type':'string','enum':['x','y']}}},'required':['s']}
        self.assertEqual(forms.validate(spec,{'s':'а','multi':['x']}),{'s':'а','multi':['x']})
        for content in ({'s':'в'},{'s':'а','multi':['x','x']},{'s':'а','extra':True},{'s':[]},{'multi':[]}):
            with self.assertRaises(HumanError):forms.validate(spec,content)
        for field in ({'type':'object'},{'type':'string','format':'password'},{'type':'array','items':{'type':'object'}}):
            with self.assertRaises(HumanError):forms.schema({'type':'object','properties':{'x':field}})
        with self.assertRaises(HumanError):forms.schema({'type':'object','properties':{'api_key':{'type':'string'}}})
        for value in (True,float('nan'),10**1000):
            with self.assertRaises(HumanError):forms.validate({'type':'object','properties':{'n':{'type':'number'}}},{'n':value})
        asyncio.run(self.app.close())
