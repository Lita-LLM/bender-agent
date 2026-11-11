"""Длинный ход с реальным HTTP/сжатием, петли, миграция и независимый Стоп."""
import asyncio
import json
from unittest.mock import patch

from bender.core.settings import Settings, NeedsOwner
from bender.core.store import Store
from bender.mind import compact
from bender.mind.loop import Turn
from bender.net.http_server import JSONResponse
from bender.tools.registry import ToolResult
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


class LongTurnTest(TempDirCase):
    def test_migration_defaults_custom_unknown_and_idempotence(self):
        store=Store(self.tmp/'old.db')
        try:
            for group,value in [('agent',{'max_steps':25,'max_turn_seconds':900,'future':7}),('team',{'max_steps':40,'max_seconds':600})]:
                store.x('INSERT INTO settings VALUES(?,?,0)',(group,json.dumps(value)))
            self.assertEqual(Settings(store).get('agent','max_steps'),25)  # doctor/Reader не мигрирует
            settings=Settings(store, migrate=True)
            self.assertEqual(settings.get('agent','max_steps'),0)
            self.assertEqual(settings.get('agent','max_turn_seconds'),0)
            self.assertEqual(settings.get('team','max_steps'),40)
            self.assertEqual(settings.get('team','max_seconds'),1800)
            self.assertEqual(settings.get('agent','future'),7)
            before=store.meta_get('settings:limits-v2');Settings(store, migrate=True)
            self.assertEqual(store.meta_get('settings:limits-v2'),before)
            settings.set('agent',{'max_steps':25})
            self.assertEqual(Settings(store).get('agent','max_steps'),25)
        finally:store.close()

    def test_old_explicit_default_from_owner_journal_is_preserved(self):
        store=Store(self.tmp/'explicit.db')
        try:
            store.x('INSERT INTO settings VALUES(?,?,0)',('agent','{"max_steps":25,"max_turn_seconds":900}'))
            store.x("INSERT INTO journal(ts,kind,actor,summary,data,hash) VALUES(0,'settings','owner','выбрал',?,'fixture')",(json.dumps({'group':'agent','diff':{'max_steps':[10,25]}}),))
            settings=Settings(store, migrate=True)
            self.assertEqual(settings.get('agent','max_steps'),25)
            self.assertEqual(settings.get('agent','max_turn_seconds'),0)
        finally:store.close()

    @async_test
    async def test_105_steps_compact_during_turn_keep_goal_dates_and_tool_pairs(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.selfdir.finish_awakening();app.settings.set('agent',{'history_tokens':2000})
            summaries=[]
            @env.fake.route('POST','/summaries/chat/completions')
            async def summary(req):
                summaries.append(req.json())
                return JSONResponse({'choices':[{'message':{'role':'assistant','content':'{"summary":"ЦЕЛЬ-СТО-ШАГОВ. Выполнены очередные пункты, продолжить оставшиеся.","diary":""}'},'finish_reason':'stop'}]})
            p=app.models.add_provider(name='Пересказы',base_url=env.base+'/summaries',extra={'is_local':True,'no_stream':True})
            app.models.set_role('fast',[{'provider':p['id'],'model':'summary'}])
            env.script([call('plan',items=[{'text':f'Пункт {i}: '+('данные '*55),'status':'doing'}]) for i in range(105)]+[{'text':'Все 105 пунктов готовы.'}])
            res=await app.mind.say(env.sid,'ЦЕЛЬ-СТО-ШАГОВ: выполни 105 разных пунктов, не теряй цель.', tainted=True)
            self.assertIsNone(res['error'],res);self.assertEqual(res['steps'],106)
            self.assertEqual(res['text'],'Все 105 пунктов готовы.')
            self.assertTrue(res['tainted'])
            self.assertTrue(compact.state(app,env.sid)['tainted'])
            self.assertGreater(len(summaries),2)
            self.assertGreater(compact.state(app,env.sid)['parts'],2)
            self.assertIn('[Период:',compact.state(app,env.sid)['summary'])
            requests=[r['body']['messages'] for r in env.fake.state.requests]
            for messages in requests:
                history=[m for m in messages if m['role']!='system']
                self.assertEqual(history[0]['role'],'user')
                pending=set()
                for m in history:
                    if m['role']=='assistant':pending.update(c['id'] for c in m.get('tool_calls') or [])
                    if m['role']=='tool':
                        self.assertIn(m['tool_call_id'],pending);pending.remove(m['tool_call_id'])
                self.assertFalse(pending)
                self.assertIn('ЦЕЛЬ-СТО-ШАГОВ',json.dumps(messages,ensure_ascii=False))
            self.assertLess(max(len(json.dumps(m,ensure_ascii=False)) for m in requests),22000)
            self.assertEqual(len([m for m in app.store.messages(env.sid,limit=1000) if m['role']=='tool']),105)

    @async_test
    async def test_abab_stops_but_changing_results_do_not(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.settings.set('memory',{'review':False})
            env.script([call('list_dir',path='a' if i%2==0 else 'b') for i in range(20)])
            result=await app.mind.say(env.sid,'сверь папки')
            self.assertEqual(result['error']['code'],'loop');self.assertEqual(result['steps'],6)
            env.script([call('plan',items=[]) for i in range(8)]+[{'text':'Готово'}])
            count=0;handler=app.tools.tools['plan'].handler
            async def changing(ctx,items):
                nonlocal count
                count+=1;return ToolResult(str(count),summary='изменился результат')
            with patch.object(app.tools.tools['plan'],'handler',changing):
                result=await app.mind.say(env.sid,'проверяй изменяющиеся данные')
            self.assertIsNone(result['error']);self.assertEqual(result['steps'],9)

    @async_test
    async def test_owner_unlimited_unattended_steps_deadline_and_stop(self):
        async with MindEnv(self.tmp) as env:
            app=env.app
            self.assertEqual(app.mind._limits(Turn(env.sid,'pult'))[:2],(0,0))
            self.assertEqual(app.mind._limits(Turn(env.sid,'cron',unattended=True))[:2],(50,1800))
            app.settings.set('agent',{'unattended_max_steps':3})
            env.script([call('plan',items=[str(i)]) for i in range(8)])
            result=await app.mind.say(env.sid,'фон',channel='cron',actor='schedule',unattended=True)
            self.assertEqual(result['steps'],3)
            started=asyncio.Event()
            async def blocked(*args,**kwargs):started.set();await asyncio.Event().wait()
            with patch.object(app.router,'call',blocked):
                result=await app.mind.say(env.sid,'таймер',limits={'seconds':.02})
                self.assertEqual(result['error']['code'],'turn_timeout')
                started.clear();await app.mind.say(env.sid,'ждать без лимита',wait=False)
                task=app.mind.turns[env.sid].task;await started.wait()
                self.assertEqual(app.mind.stop(env.sid),1)
                result=await task;self.assertEqual(result['error']['code'],'stopped')

    @async_test
    async def test_stop_inside_tool_closes_pair_and_keeps_owner_inbox(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;started=asyncio.Event()
            async def blocked(ctx,items):started.set();await asyncio.Event().wait()
            env.script([call('plan',items=['работать'])])
            with patch.object(app.tools.tools['plan'],'handler',blocked):
                await app.mind.say(env.sid,'начни',wait=False)
                task=app.mind.turns[env.sid].task;await started.wait()
                await app.mind.say(env.sid,'Уточнение не терять',wait=False)
                app.mind.stop(env.sid);await task
            rows=[m for m in app.store.messages(env.sid) if m['role']!='event']
            self.assertEqual([m['role'] for m in rows],['user','assistant','tool','user'])
            self.assertEqual(rows[-1]['content'],'Уточнение не терять')
            self.assertEqual(rows[-2]['tool_call_id'],rows[-3]['tool_calls'][0]['id'])
            self.assertTrue(rows[-2]['is_error'])
