"""Получатель видит нить и итог, но хозяин не превращает переписку в обычный чат."""
import asyncio
import json

from bender.core.errors import HumanError
from bender.net.http_server import HttpServer
from bender.tools.registry import ToolContext,ToolResult
from tests.helpers import TempDirCase,async_test,owner_web,owner_headers
from tests.test_mind import MindEnv,call
from tests import test_telegram as telegram
from tests import test_agent_messages as letters


class CorrespondenceViewTest(TempDirCase):
    prepare=letters.AgentMessagesTest.prepare
    links=letters.AgentMessagesTest.links
    until=telegram.TelegramTest.until
    sent=telegram.TelegramTest.sent

    async def deliver(self,env):
        app,other=self.prepare(env);self.links(app)
        env.script([call('list_dir'),{'text':'Проверил и ответил.'}])
        result=await app.tools.run('message_agent',{'agent':'second','text':'Проверь свою папку','wait':True},ToolContext(app,session_id=env.sid))
        self.assertTrue(result.ok,result.content)
        row=app.agents.correspondence.get(result.data['id'])
        return app,other,row

    @async_test
    async def test_recipient_thread_notice_and_switch_without_new_main_turn(self):
        async with MindEnv(self.tmp) as env:
            app,other,row=await self.deliver(env)
            thread=other.store.session(row['session_id']);self.assertEqual(thread['meta']['correspondence_peer'],'main')
            messages=other.store.messages(thread['id'])
            self.assertEqual([m['role'] for m in messages],['user','assistant','tool','assistant'])
            self.assertEqual(messages[0]['meta']['correspondence_sender'],{'id':'main','name':'Главный'})
            self.assertTrue(messages[0]['meta']['clean_letter']);self.assertFalse(messages[0]['meta'].get('untrusted'));self.assertEqual(messages[-1]['content'],'Проверил и ответил.')
            notice=other.store.messages(other.main_session());self.assertEqual(len(notice),1)
            self.assertEqual(notice[0]['meta']['correspondence_notice']['session'],thread['id'])
            self.assertFalse(other.mind.running(other.main_session()));self.assertFalse(app.store.messages(env.sid))
            self.assertEqual(len([r for r in env.fake.state.requests if r.get('api')=='openai']),2)
            app.settings.set('agent_chat',{'main_notices':False})
            env.script([{'text':'Второй ответ.'}])
            result=await app.tools.run('message_agent',{'agent':'second','text':'Ещё','wait':True},ToolContext(app,session_id=env.sid))
            self.assertTrue(result.ok,result.content);self.assertEqual(other.store.messages(other.main_session()),notice)

    @async_test
    async def test_readonly_owner_api_and_cross_agent_isolation(self):
        async with MindEnv(self.tmp) as env:
            app,other,row=await self.deliver(env);sid=row['session_id'];before=other.store.messages(sid)
            web=HttpServer(owner_web(app,allowed_hosts={'127.0.0.1'}));await web.start('127.0.0.1',0)
            headers=owner_headers(**{'X-Bender':'1','X-Bender-Agent':'second'})
            base='http://127.0.0.1:'+str(web.port)
            try:
                async with await app.http.get(base+'/api/chats',headers=headers) as response:data=await response.json()
                self.assertTrue(next(s for s in data['sessions'] if s['id']==sid)['read_only'])
                async with await app.http.get(base+'/api/session/'+sid+'/messages',headers=headers) as response:data=await response.json()
                self.assertEqual(data['session']['kind'],'correspondence');self.assertEqual(len(data['messages']),4)
                async with await app.http.post(base+'/api/say',headers=headers,json={'session':sid,'text':'Подмени письмо'}) as response:self.assertEqual(response.status,403)
                async with await app.http.request('PATCH',base+'/api/chats/'+sid,headers=headers,json={'title':'Подмена'}) as response:self.assertEqual(response.status,403)
                with self.assertRaises(HumanError):await other.mind.say(sid,'Подмени',actor='owner')
                self.assertEqual(other.store.messages(sid),before)
                async with await app.http.get(base+'/api/session/'+sid+'/messages',headers=owner_headers()) as response:data=await response.json()
                self.assertFalse(data['messages']);self.assertIsNone(data['session'])
                self.assertEqual(len([r for r in env.fake.state.requests if r.get('api')=='openai']),2)
            finally:await web.stop()

    @async_test
    async def test_telegram_chats_has_separate_readonly_section_even_after_eighty_chats(self):
        async with MindEnv(self.tmp) as env:
            app,other,row=await self.deliver(env);await telegram.TelegramTest._up(self,env,owner=True);tg=app.telegram
            tg.choose(42,other)
            for index in range(85):other.store.new_session('chat','Обычный '+str(index))
            await tg._handle({'message':{'message_id':2001,'from':{'id':42},'chat':{'id':42,'type':'private'},'text':'/chats'}})
            button=next(b for line in self.sent(env)[-1]['reply_markup']['inline_keyboard'] for b in line if b['text'].startswith('Переписка'))
            async def click(data):await tg._callback({'id':'read','from':{'id':42},'message':{'chat':{'id':42},'message_id':33},'data':data})
            await click(button['callback_data'])
            thread=next(b for line in self.sent(env)[-1]['reply_markup']['inline_keyboard'] for b in line if b['text'].startswith('Переписка с Главный'))
            await click(thread['callback_data'])
            text=self.sent(env)[-1]['text'];self.assertIn('только чтение',text);self.assertIn('Главный',text);self.assertIn('list_dir',text);self.assertIn('Проверил и ответил.',text)
            self.assertEqual(tg.selected(42),(other,other.main_session()))
            self.assertEqual(len([r for r in env.fake.state.requests if r.get('api')=='openai']),2)

    @async_test
    async def test_notice_waits_for_active_recipient_tool_and_does_not_become_another_task(self):
        async with MindEnv(self.tmp) as env:
            app,other=self.prepare(env);self.links(app)
            entered=asyncio.Event();release=asyncio.Event()
            @other.tools.tool('letter_wait','Подождать',group='files')
            async def waiting(ctx):entered.set();await release.wait();return ToolResult('Ожидание завершено')
            other.settings.set('agent_rights',{'profile':'full'})
            env.script([call('letter_wait'),{'text':'Ответ на письмо.'},{'text':'Закончил свою работу.'}])
            await other.mind.say(other.main_session(),'Займись своей задачей',wait=False)
            await asyncio.wait_for(entered.wait(),3)
            result=await app.tools.run('message_agent',{'agent':'second','text':'Короткий вопрос','wait':True},ToolContext(app,session_id=env.sid))
            self.assertTrue(result.ok,result.content)
            self.assertFalse(any(m['meta'].get('correspondence_notice') for m in other.store.messages(other.main_session())))
            release.set();await self.until(lambda:not other.mind.running(other.main_session()))
            rows=other.store.messages(other.main_session())
            self.assertEqual([r['role'] for r in rows],['user','assistant','tool','user','assistant'])
            self.assertTrue(rows[3]['meta']['correspondence_notice']);self.assertEqual(rows[-1]['content'],'Закончил свою работу.')
            self.assertEqual(len([r for r in env.fake.state.requests if r.get('api')=='openai']),3)
