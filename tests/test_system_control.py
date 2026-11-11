"""Перезапуск/выключение всей стаи, подтверждение хозяина и одна копия данных."""
import asyncio
import os
import signal
import subprocess
import sys
from unittest.mock import patch,Mock

from bender.core.errors import HumanError
from bender.core.paths import PROGRAM_DIR
from bender.core.run_lock import RunLock,current
from bender.guard.policy import Action
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv
from tests.test_watchdog import _api,_wait_state,_free_port,_API_DATA
from tests import test_telegram as original


class LifecycleTest(TempDirCase):
    until=original.TelegramTest.until
    sent=original.TelegramTest.sent

    @async_test
    async def test_agent_restart_tool_finishes_its_own_turn_before_restart(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.watched=True;app.request_restart=Mock();cards=[]
            def approve(event):
                if event.get('type')=='approval':cards.append(event['card']);app.guard.respond(event['card']['id'],'once')
            app.bus.listen(approve)
            env.script([{'tool_calls':[{'name':'restart','arguments':{'reason':'Проверка перезапуска из хода'}}]},{'text':'Перезапуск подтверждён хозяином.'}])
            result=await app.mind.say(env.sid,'Перезапустись после ответа')
            self.assertIsNone(result['error']);self.assertIn('подтверждён',result['text'])
            await asyncio.wait_for(app.lifecycle.task,3)
            app.request_restart.assert_called_once();self.assertEqual(len(cards),1)
            self.assertIn('Проверка перезапуска из хода',cards[0]['summary'])

    @async_test
    async def test_telegram_owner_confirmation_same_topic_return_and_cancel(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.watched=True;app.request_restart=Mock();app.request_shutdown=Mock()
            env.fake.state.tg_topics=True
            await original.TelegramTest._up(self,env,owner=True)
            tg=app.telegram
            from bender.channels.telegram_topics import Address
            await tg.commands.execute(app,Address(42),env.sid,'new','Управление')
            row=next(iter(tg.topics.rows().values()));address=Address(42,row['thread'])
            await tg.commands.execute(app,address,row['session'],'restart','')
            card=self.sent(env)[-1];data=card['reply_markup']['inline_keyboard'][0][0]['callback_data']
            message={'chat':{'id':42,'type':'private'},'message_thread_id':row['thread']}
            await tg._callback({'id':'foreign','from':{'id':7},'message':message,'data':data})
            app.request_restart.assert_not_called()
            await tg._callback({'id':'owner','from':{'id':42},'message':message,'data':data})
            await asyncio.wait_for(app.lifecycle.task,3);app.request_restart.assert_called_once()
            before=len(self.sent(env));await tg._restart_notice()
            notice=self.sent(env)[-1]
            self.assertIn('Снова тут',notice['text']);self.assertEqual(notice['chat_id'],42)
            self.assertEqual(notice['message_thread_id'],row['thread'])
            await tg._restart_notice();self.assertEqual(len(self.sent(env)),before+1)
            await tg._callback({'id':'repeat','from':{'id':42},'message':message,'data':data})
            app.request_restart.assert_called_once()
            app._draining=False
            await tg.commands.execute(app,address,row['session'],'shutdown','')
            cancel=self.sent(env)[-1]['reply_markup']['inline_keyboard'][2][0]['callback_data']
            await tg._callback({'id':'cancel','from':{'id':42},'message':message,'data':cancel})
            app.request_shutdown.assert_not_called()
            self.assertFalse(any(r.get('api')=='openai' for r in env.fake.state.requests))

    @async_test
    async def test_restart_always_asks_even_with_off_grant_and_always_answer(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.settings.set('guard',{'profile':'off'})
            app.guard.grant(env.sid,[('system.restart','*'),('system.shutdown','*')])
            cards=[]
            def approve(event):
                if event.get('type')=='approval':
                    cards.append(event['card']);app.guard.respond(event['card']['id'],'always')
            app.bus.listen(approve)
            for _ in range(2):self.assertTrue((await app.guard.check(Action('system.restart','данные'),session_id=env.sid)).allowed)
            self.assertEqual(len(cards),2);self.assertFalse(any(card['can_remember'] for card in cards))
            self.assertFalse(any(r['capability']=='system.restart' for r in app.guard.rules()))
            for cap in ('system.shutdown','system.service'):
                self.assertFalse((await app.guard.check(Action(cap,'данные'),session_id=env.sid)).allowed)

    @async_test
    async def test_wait_keeps_active_turn_but_refuses_new_turn_then_restarts(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.watched=True;called=asyncio.Event();app.request_restart=called.set
            started=asyncio.Event();release=asyncio.Event();original=app.router.call
            async def held(*args,**kw):started.set();await release.wait();return await original(*args,**kw)
            env.script([{'text':'Завершил'}])
            with patch.object(app.router,'call',held):
                turn=asyncio.create_task(app.mind.say(env.sid,'Работай'))
                await started.wait()
                result=await app.lifecycle.request(ToolContext(app,actor='owner'),'restart')
                self.assertNotIn('systemctl',result['message'])
                with self.assertRaises(HumanError):await app.mind.say(env.sid,'Новый ход')
                await asyncio.sleep(.55);self.assertFalse(called.is_set());self.assertFalse(turn.done())
                release.set();await turn;await asyncio.wait_for(app.lifecycle.task,3)
                self.assertTrue(called.is_set())

    @async_test
    async def test_interrupt_and_restart_fields(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.request_shutdown=lambda:None;app.settings.set('web',{'port':9982})
            self.assertIn('web.port',app.lifecycle.state()['restart_fields'])
            started=asyncio.Event()
            async def held(*args,**kw):started.set();await asyncio.Event().wait()
            with patch.object(app.router,'call',held):
                turn=asyncio.create_task(app.mind.say(env.sid,'Долго'))
                await started.wait()
                await app.lifecycle.request(ToolContext(app,actor='owner'),'shutdown',policy='interrupt')
                await asyncio.wait_for(app.lifecycle.task,3)
                await asyncio.gather(turn,return_exceptions=True)
                self.assertFalse(app.mind.running(env.sid))

    def test_second_copy_names_pid_and_release_allows_next_start(self):
        data=self.tmp/'data'
        with RunLock(data):
            self.assertEqual(current(data)['pid'],os.getpid())
            result=subprocess.run([sys.executable,'-m','bender','--data',str(data),'run','--no-watchdog','--no-browser'],cwd=PROGRAM_DIR,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('PID '+str(os.getpid()),result.stdout+result.stderr)
            self.assertIn('kill -TERM',result.stdout+result.stderr)
        self.assertIsNone(current(data))
        with RunLock(data):self.assertEqual(current(data)['pid'],os.getpid())

    def launch(self,watched=True):
        data=self.tmp/'data';port=_free_port();_API_DATA[port]=data
        env={k:v for k,v in os.environ.items() if k not in ('BENDER_CORE','BENDER_WATCHED','BENDER_SAFE','BENDER_RESTARTED','BENDER_SERVICE_KIND','BENDER_SERVICE_DATA')}
        argv=[sys.executable,'-m','bender','--data',str(data),'--no-anim','run','--port',str(port),'--no-browser']
        if not watched:argv+=['--no-watchdog']
        process=subprocess.Popen(argv,cwd=PROGRAM_DIR,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        return data,port,process

    def test_manual_watchdog_restart_then_shutdown_exit_zero(self):
        data,port,process=self.launch()
        try:
            first=_wait_state(port);self.assertIsNotNone(first)
            holder=current(data)
            other=subprocess.run([sys.executable,'-m','bender','--data',str(data),'run','--no-browser'],cwd=PROGRAM_DIR,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(other.returncode,0);self.assertIn('PID '+str(holder['pid']),other.stdout+other.stderr)
            self.assertEqual(_api(port,'/api/state')['boot']['starts'],first['boot']['starts'])
            result=_api(port,'/api/control/restart',{'confirmed':True,'policy':'wait'})
            self.assertNotIn('systemctl',result['message'])
            second=_wait_state(port,lambda row:row['boot']['starts']>first['boot']['starts'])
            self.assertIsNotNone(second)
            result=_api(port,'/api/control/shutdown',{'confirmed':True,'policy':'interrupt'})
            self.assertIn('Запустить снова: bender.',result['message']);self.assertNotIn('systemctl',result['message'])
            process.communicate(timeout=12);self.assertEqual(process.returncode,0)
            self.assertIsNone(current(data))
        finally:
            if process.poll() is None:process.send_signal(signal.SIGTERM);process.communicate(timeout=12)

    def test_without_watchdog_restart_is_honest_and_shutdown_stops_core(self):
        data,port,process=self.launch(watched=False)
        try:
            self.assertIsNotNone(_wait_state(port))
            from urllib.error import HTTPError
            with self.assertRaises(HTTPError) as failed:_api(port,'/api/control/restart',{'confirmed':True})
            self.assertEqual(failed.exception.code,409)
            failed.exception.close()
            _api(port,'/api/control/shutdown',{'confirmed':True})
            process.communicate(timeout=12);self.assertEqual(process.returncode,0)
            self.assertIsNone(current(data))
        finally:
            if process.poll() is None:process.send_signal(signal.SIGTERM);process.communicate(timeout=12)
