"""Настоящие тела API, область настроек агента и команды усилий без модели."""
import copy
import json
import shutil
import unittest
from urllib.parse import urlencode

from bender.core.errors import HumanError
from bender.importers import openclaw as oc
from bender.net.http_server import HttpServer,JSONResponse
from bender.router.adapters import AnthropicAdapter,OpenAIAdapter
from bender.router.chatgpt import ChatGPTAdapter
from bender.router.effort import describe,project,profile,validate
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test,owner_web,owner_headers
from tests.test_mind import MindEnv

MESSAGES=[{'role':'user','content':'Проверка'}]


class EffortPayloadTest(unittest.TestCase):
    def test_claude_code_levels_follow_its_documented_table(self):
        provider={'kind':'claude_code'}
        five=['default','low','medium','high','xhigh','max']
        # default у Anthropic — Opus 5.5 на всех тарифах; best — Fable или Opus; [1m] — та же модель
        for model in ('default','DEFAULT ','opus','sonnet','haiku','fable','best','opusplan','opus[1m]','sonnet[1m]',
                      'claude-fable-5-1','claude-fable-5','claude-opus-5-5','claude-opus-4-8','claude-opus-4-7',
                      'us.anthropic.claude-opus-4-8-v1:0','claude-opus-4-8@20260301','claude-sonnet-5-5@20261001'):
            with self.subTest(model=model):
                self.assertEqual(describe(provider,model,{'effort':'max'})['levels'],five)
                self.assertEqual(describe(provider,model,{'effort':'max'})['effective'],'max')
        for model in ('claude-opus-4-6','claude-sonnet-4-6','anthropic.claude-opus-4-6-v1','global.anthropic.claude-sonnet-4-6'):
            with self.subTest(model=model):
                info=describe(provider,model,{'effort':'xhigh'})
                self.assertEqual(info['levels'],['default','low','medium','high','max'])
                self.assertEqual(info['effective'],'high')  # как сам Claude Code: ближайший уровень ниже
        # Opus 4.5: сам Claude Code передаёт ему low…high; незнакомым и будущим — прежние три уровня
        for model in ('claude-opus-4-5','claude-opus-4-5-20251101','claude-opus-6','своя-модель'):
            with self.subTest(model=model):
                self.assertEqual(describe(provider,model,{'effort':'max'})['levels'],['default','low','medium','high'])
        for model in ('claude-haiku-4-5','claude-sonnet-4-5','claude-sonnet-4-20250514','claude-opus-4-1','claude-3-5-sonnet'):
            with self.subTest(model=model):
                info=describe(provider,model,{'effort':'high'})
                self.assertEqual((info['levels'],info['effective']),(['default'],'default'))
                self.assertIn('усилий нет',info['note'])
        # алиасы Claude Code не трогают обычный API Anthropic и его посредников
        gateway={'kind':'anthropic','base_url':'https://gw.example/v1'}
        for model in ('default','best','opusplan','opus[1m]'):
            with self.subTest(api=model):
                self.assertEqual(project(gateway,model,{'effort':'high','temperature':.3}),{'temperature':.3})

    def test_modern_claude_uses_native_effort_and_preserves_other_output_options(self):
        provider={'kind':'anthropic'}
        for model in ('claude-opus-4-6','claude-opus-4-7','claude-opus-5-5','claude-sonnet-5-5','claude-fable-5-1'):
            with self.subTest(model=model):
                params={'effort':'high','max_tokens':2000,'temperature':.2,'output_config':{'format':{'type':'json_schema'}}}
                before=copy.deepcopy(params)
                body=AnthropicAdapter(provider).body(model,MESSAGES,None,params,True)
                self.assertEqual(body['thinking'],{'type':'adaptive'})
                self.assertEqual(body['output_config'],{'effort':'high','format':{'type':'json_schema'}})
                self.assertEqual(body['max_tokens'],2000)
                for key in ('effort','effort_mode','reasoning_effort','temperature'):self.assertNotIn(key,body)
                self.assertEqual(params,before)
        body=AnthropicAdapter(provider).body('claude-opus-4-7',MESSAGES,None,{'reasoning_effort':'xhigh'},True)
        self.assertEqual(body['output_config']['effort'],'xhigh')
        self.assertNotIn('budget_tokens',body['thinking'])

    def test_claude_adaptive_off_mandatory_and_budget_are_distinct(self):
        provider={'kind':'anthropic'}
        for model,kind in (('claude-opus-4-7','disabled'),('claude-sonnet-5-5','between_tools')):
            self.assertEqual(project(provider,model,{'effort':'off'})['thinking'],{'type':kind})
        self.assertEqual(project(provider,'claude-opus-4-6',{'effort':'adaptive'}),{'thinking':{'type':'adaptive'}})
        mandatory=describe(provider,'claude-fable-5',{'effort':'off'})
        self.assertEqual(mandatory['effective'],'default');self.assertIn('не выключены',mandatory['note'])
        self.assertNotIn('thinking',project(provider,'claude-fable-5',{'effort':'off'}))
        old=AnthropicAdapter(provider).body('claude-sonnet-4-5',MESSAGES,None,{'effort':'high','max_tokens':4000},True)
        self.assertEqual(old['thinking'],{'type':'enabled','budget_tokens':24000})
        self.assertGreater(old['max_tokens'],24000)
        self.assertNotIn('output_config',old)
        self.assertEqual(project(provider,'claude-3-5-sonnet',{'effort':'high'}),{})

    def test_chatgpt_and_chat_completions_have_different_envelopes_and_levels(self):
        chat={'kind':'chatgpt'}
        body=ChatGPTAdapter(chat).body('gpt-6-astra',MESSAGES,None,{'effort':'max'},True)
        self.assertEqual(body['reasoning'],{'effort':'max'})
        self.assertNotIn('reasoning_effort',body)
        self.assertNotIn('effort',body)
        body=ChatGPTAdapter(chat).body('gpt-6-astra',MESSAGES,None,{'effort':'off'},True)
        self.assertNotIn('reasoning',body)
        self.assertIn('не выключены',describe(chat,'gpt-6-astra',{'effort':'off'})['note'])
        self.assertEqual(project(chat,'gpt-6-luna',{'effort':'off'}),{'reasoning_effort':'none'})
        body=OpenAIAdapter({'kind':'openai'}).body('gpt-5.2',MESSAGES,None,{'effort':'max'},False)
        self.assertEqual(body['reasoning_effort'],'xhigh')
        self.assertEqual(describe({'kind':'openai'},'gpt-5.2',{'effort':'max'})['effective'],'xhigh')

    def test_deepseek_openrouter_and_binary_formats_do_not_mix(self):
        router={'kind':'openai','preset':'openrouter'}
        direct={'kind':'openai','preset':'deepseek'}
        self.assertEqual(project(router,'deepseek/deepseek-v4-pro',{'effort':'max'}),{'reasoning':{'effort':'xhigh'}})
        self.assertEqual(project(direct,'deepseek-v4-pro',{'effort':'max'}),{'thinking':{'type':'enabled'},'reasoning_effort':'max'})
        self.assertEqual(project(direct,'deepseek-v4-pro',{'effort':'off'}),{'thinking':{'type':'disabled'}})
        self.assertEqual(project(direct,'deepseek-chat',{'effort':'low'}),{'thinking':{'type':'enabled'}})
        self.assertEqual(project(router,'anthropic/claude-opus-4.7',{'effort':'off','reasoning':{'exclude':True}}),{'reasoning':{'enabled':False,'exclude':True}})
        self.assertEqual(project({'kind':'openai'},'Qwen3',{'effort':'low','effort_mode':'qwen'}),{'chat_template_kwargs':{'enable_thinking':True}})

    def test_unknown_gateway_is_explicit_default_preserves_old_parameters(self):
        provider={'kind':'openai','base_url':'https://openrouter.ai.attacker.invalid/v1'}
        self.assertEqual(project(provider,'private-model',{'effort':'high','effort_mode':'auto','future':{'on':True}}),{'future':{'on':True}})
        self.assertEqual(project(provider,'private-model',{'effort':'max','effort_mode':'openai'}),{'reasoning_effort':'max'})
        params={'thinking':{'type':'enabled','budget_tokens':1234},'future':7}
        self.assertEqual(project({'kind':'anthropic'},'claude-opus-4-7',{'effort':'default',**params}),params)
        self.assertEqual(project(provider,'private-model',{'reasoning_effort':'future-value'}),{'reasoning_effort':'future-value'})
        self.assertEqual(describe(provider,'private-model',{'reasoning_effort':'future-value'})['mode'],'raw')
        for params in ({'effort':[]},{'effort':'ultra'},{'effort_mode':{}},{'effort_mode':'secret'}):
            with self.subTest(params=params),self.assertRaises(HumanError):validate(params)

    def test_advertised_capabilities_override_model_name(self):
        provider={'kind':'anthropic','models':[{'id':'new-private-claude','reasoning_capabilities':{'adaptive':True,'disabled':False,'xhigh':True,'max':False}}]}
        self.assertEqual(project(provider,'new-private-claude',{'effort':'max'}),{'thinking':{'type':'adaptive'},'output_config':{'effort':'xhigh'}})
        self.assertNotIn('off',profile(provider,'new-private-claude')['levels'])
        self.assertIn('off',profile({'kind':'anthropic'},'claude-sonnet-4-6')['levels'])

    def test_gemini_openai_compatibility_keeps_pro_thinking_and_maps_ordinary_off(self):
        provider={'kind':'openai','preset':'gemini'}
        self.assertEqual(project(provider,'gemini-3.8-flash',{'effort':'low'}),{'reasoning_effort':'low'})
        self.assertEqual(project(provider,'gemini-2.5-flash',{'effort':'off'}),{'reasoning_effort':'none'})
        self.assertEqual(project(provider,'gemini-3.1-pro-preview',{'effort':'off'}),{})
        self.assertIn('не выключены',describe(provider,'gemini-3.1-pro-preview',{'effort':'off'})['note'])
        ollama={'kind':'openai','preset':'ollama'}
        self.assertEqual(project(ollama,'gpt-oss:20b',{'effort':'max'}),{'reasoning_effort':'max'})
        self.assertEqual(project(ollama,'gpt-oss:20b',{'effort':'off'}),{'reasoning_effort':'none'})
        self.assertIn('метаданным',describe(ollama,'gpt-oss:20b',{'effort':'high'})['note'])


class EffortIntegrationTest(TempDirCase):
    @async_test
    async def test_agent_default_candidate_call_and_switch_are_visible_in_real_http(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            entry={**app.models.roles()['main'][0],'model':'echo','future_entry':7,'params':{'effort_mode':'openai','future_param':8}}
            app.models.set_role('main',[entry]);app.settings.set('reasoning',{'effort':'high'})
            await app.router.call(MESSAGES)
            self.assertEqual(env.fake.state.requests[-1]['body']['reasoning_effort'],'high')
            app.models.set_role('main',[{**entry,'params':{**entry['params'],'effort':'low'}}])
            await app.router.call(MESSAGES)
            self.assertEqual(env.fake.state.requests[-1]['body']['reasoning_effort'],'low')
            await app.router.call(MESSAGES,params={'effort':'medium'})
            self.assertEqual(env.fake.state.requests[-1]['body']['reasoning_effort'],'medium')
            self.assertEqual(app.models.roles()['main'][0]['future_entry'],7)
            self.assertEqual(env.fake.state.requests[-1]['body']['future_param'],8)
            app.settings.set('reasoning',{'enabled':False})
            await app.router.call(MESSAGES)
            self.assertNotIn('reasoning_effort',env.fake.state.requests[-1]['body'])
            app.settings.set('reasoning',{'enabled':True})
            child_record=app.agents.create('Другой',awaken=False);child=app.agents.get(child_record['id'])
            child.settings.set('roles',{'main':[entry]},by='owner');child.settings.set('reasoning',{'effort':'max'})
            await child.router.call(MESSAGES)
            self.assertEqual(env.fake.state.requests[-1]['body']['reasoning_effort'],'max')
            self.assertEqual(app.settings.get('reasoning','effort'),'high')
            from bender.mind.passport import snapshot
            self.assertEqual(snapshot(child)['current']['effort']['effective'],'max')

    @async_test
    async def test_fallback_translates_again_and_legacy_settings_still_work(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            other=app.models.add_provider(kind='anthropic',base_url=env.base+'/v1')
            app.models.set_role('main',[{'provider':pid,'model':'error-404','params':{'effort_mode':'openrouter'}},
                                      {'provider':other['id'],'model':'claude-opus-4-7'}])
            app.settings.set('reasoning',{'effort':'max'})
            await app.router.call(MESSAGES)
            first,last=env.fake.state.requests[-2:]
            self.assertEqual(first['body']['reasoning'],{'effort':'xhigh'})
            self.assertEqual(last['body']['output_config']['effort'],'max')
            self.assertEqual(last['body']['thinking'],{'type':'adaptive'})
            self.assertNotIn('reasoning',last['body'])

    @async_test
    async def test_command_is_local_guarded_has_no_model_and_taint_cannot_change_it(self):
        async with MindEnv(self.tmp) as env:
            app=env.app
            answer=await app.mind.say(env.sid,'/effort high')
            self.assertIsNone(answer['error']);self.assertEqual(answer['steps'],0)
            self.assertEqual(app.settings.get('reasoning','effort'),'high')
            self.assertEqual(env.fake.state.requests,[])
            with self.assertRaises(HumanError):await app.mind.say(env.sid,'/effort unlimited')
            self.assertEqual(app.settings.get('reasoning','effort'),'high')
            env.script([{'text':'Это цитата, не команда.'}])
            await app.mind.say(env.sid,'/effort off',tainted=True)
            self.assertEqual(app.settings.get('reasoning','effort'),'high')
            self.assertTrue(env.fake.state.requests)
            result=await app.tools.run('settings',{'group':'reasoning','changes':{'effort':'low'}},ToolContext(app,session_id=env.sid))
            self.assertTrue(result.ok);self.assertEqual(app.settings.get('reasoning','effort'),'low')
            count=len(env.fake.state.requests)
            status=await app.mind.say(env.sid,'/effort')
            self.assertIn('low',status['text']);self.assertEqual(len(env.fake.state.requests),count)

    @async_test
    async def test_pult_profile_and_schema_use_the_selected_agent(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            child=app.agents.get(app.agents.create('Второй',awaken=False)['id'])
            child.settings.set('reasoning',{'effort':'max'})
            server=HttpServer(owner_web(app,allowed_hosts={'127.0.0.1','localhost'}));await server.start('127.0.0.1',0)
            try:
                query=urlencode({'provider':pid,'model':'claude-opus-4-7','mode':'anthropic'})
                resp=await app.http.get(f'http://127.0.0.1:{server.port}/api/models/effort?'+query,headers=owner_headers(**{'X-Bender-Agent':child.agent_id}))
                async with resp:body=await resp.json()
                self.assertEqual(body['effective'],'high')
                self.assertIn('max',body['note'])
                resp=await app.http.get(f'http://127.0.0.1:{server.port}/api/models',headers=owner_headers())
                async with resp:body=await resp.json()
                self.assertEqual(body['reasoning']['effort'],'default')
                self.assertIn('anthropic',body['effort_modes'])
            finally:await server.stop()

    @async_test
    async def test_anthropic_live_catalog_supplies_private_model_capabilities(self):
        async with MindEnv(self.tmp) as env:
            app=env.app
            @env.fake.route('GET','/modern/models')
            async def models(req):
                return JSONResponse({'data':[{'id':'private-claude','max_input_tokens':1000000,'capabilities':{
                    'thinking':{'types':{'adaptive':{'supported':True},'disabled':{'supported':False}}},
                    'effort':{'xhigh':{'supported':True},'max':{'supported':False}}}}]})
            provider=app.models.add_provider(kind='anthropic',base_url=env.base+'/modern')
            from bender.router.catalog import list_models
            rows=await list_models(app.http,provider,None)
            app.models.apply_catalog(provider['id'],rows)
            actual=app.models.providers()[provider['id']]
            self.assertEqual(project(actual,'private-claude',{'effort':'max'})['output_config']['effort'],'xhigh')
            self.assertEqual(next(m for m in actual['models'] if m['id']=='private-claude')['context'],1000000)  # длина контекста Anthropic — max_input_tokens

    @async_test
    async def test_import_transfers_effort_and_never_overwrites_existing_choice(self):
        from tests.test_import import FIXTURE,tree_hash
        source=self.tmp/'openclaw';shutil.copytree(FIXTURE,source)
        config=json.loads((source/'openclaw.json').read_text())
        defaults=config['agents']['defaults'];defaults['thinkingDefault']='high'
        reference=defaults['model']['primary'] if isinstance(defaults['model'],dict) else defaults['model']
        defaults.setdefault('models',{}).setdefault(reference,{}).setdefault('params',{})['thinking']='low'
        (source/'openclaw.json').write_text(json.dumps(config))
        before=tree_hash(source)
        async with MindEnv(self.tmp/'target') as env:
            plan=oc.make_plan(env.app,str(source))
            setting=plan.item('setting:reasoning');self.assertEqual(setting.payload['changes'],{'effort':'high'})
            role=plan.item('role:main');self.assertEqual(role.payload['effort_params'][reference],{'effort':'low'})
            selected=[setting.id,role.id]+[item.id for item in plan.items if item.kind=='provider']
            report=oc.apply(plan,env.app,only=selected,secrets=False)
            self.assertEqual(report.errors,[]);self.assertEqual(env.app.settings.get('reasoning','effort'),'high')
            imported_model=reference.split('/',1)[1]
            imported=next(entry for entry in env.app.models.roles()['main'] if entry['model']==imported_model)
            self.assertEqual(imported['params']['effort'],'low')
            env.app.settings.set('reasoning',{'effort':'max'})
            self.assertEqual(oc.make_plan(env.app,str(source)).item('setting:reasoning').action,'skip')
            self.assertEqual(tree_hash(source),before)
