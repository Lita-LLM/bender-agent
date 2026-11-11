"""Темы: раздельные адреса ходов, медиа, восстановление и потерянный номер."""
import asyncio
from unittest.mock import patch
from bender.channels.telegram_topics import Address
from bender.core.errors import HumanError
from bender.guard.policy import Action
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv,call
from tests import test_telegram as original


class TopicsTest(TempDirCase):
    until=original.TelegramTest.until
    sent=original.TelegramTest.sent

    async def up(self,env):
        env.fake.state.tg_topics=True
        await original.TelegramTest._up(self,env,owner=True)
        return env.app.telegram

    async def message(self,tg,text='',thread=None,mid=900,**extra):
        message={'message_id':mid,'chat':{'id':42,'type':'private'},'from':{'id':42},'text':text,**extra}
        if thread is not None:message.update(message_thread_id=thread,is_topic_message=True)
        await tg._handle({'message':message})

    def rows(self,tg):return [row for row in tg.topics.rows().values() if not row.get('replacement')]

    @async_test
    async def test_new_topics_concurrent_agents_general_and_drafts(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('telegram',{'live_via':'draft'})  # проверяется именно черновик Telegram
            tg=await self.up(env);app=env.app
            await self.message(tg,'/new Первый разговор')
            first=self.rows(tg)[0]
            child=app.agents.get(app.agents.create('Сосед',awaken=False)['id'])
            tg.choose(42,child)
            await self.message(tg,'/new Второй разговор')
            second=self.rows(tg)[1]
            self.assertEqual(second['agent'],child.agent_id)
            self.assertEqual(tg.selected(42),(child,child.main_session()))
            env.script([{'text':'Ответ первой темы.','reasoning':'Думаю в первой','delay':.2},{'text':'Ответ второй темы.','delay':.1}])
            await self.message(tg,'Слово первой темы',first['thread'],mid=901)
            await asyncio.sleep(.01)
            await self.message(tg,'Слово второй темы',second['thread'],mid=902)
            await self.until(lambda:len([m for m in self.sent(env) if m.get('text','').startswith('Ответ ')])==2)
            await self.until(lambda:not tg._bg)
            replies={m['text']:m for m in self.sent(env) if m.get('text','').startswith('Ответ ')}
            self.assertEqual(replies['Ответ первой темы.']['message_thread_id'],first['thread'])
            self.assertEqual(replies['Ответ второй темы.']['message_thread_id'],second['thread'])
            for ctx,sid in ((app,env.sid),(child,child.main_session())):
                rows=ctx.store.messages(sid)
                self.assertTrue(rows);self.assertTrue(all(row['meta'].get('service_event') for row in rows))
                self.assertFalse(ctx.mind.running(sid))
            self.assertIn('Слово первой темы',app.store.messages(first['session'])[0]['content'])
            self.assertIn('Слово второй темы',child.store.messages(second['session'])[0]['content'])
            drafts=self.sent(env,'sendMessageDraft');self.assertTrue(drafts)
            self.assertTrue(all(m.get('message_thread_id') in (first['thread'],second['thread']) for m in drafts))
            self.assertTrue(all(m.get('message_thread_id') in (first['thread'],second['thread']) for m in self.sent(env,'sendChatAction')))
            env.script([{'text':'Ответ общего потока.'}]);await self.message(tg,'Общее сообщение',mid=903)
            await self.until(lambda:any(m.get('text')=='Ответ общего потока.' for m in self.sent(env)))
            general=next(m for m in self.sent(env) if m.get('text')=='Ответ общего потока.')
            self.assertNotIn('message_thread_id',general)
            self.assertIn('Общее сообщение',next(r for r in child.store.messages(child.main_session()) if not r['meta'].get('service_event'))['content'])

    @async_test
    async def test_files_reactions_voice_and_title_keep_topic(self):
        async with MindEnv(self.tmp) as env:
            tg=await self.up(env);app=env.app
            sid=app.store.new_session('chat','Файлы');address=await tg.topics.create(app,42,sid)
            (app.dirs.work/'out.txt').write_text('Содержимое файла')
            env.script([call('send_file',path='out.txt'),call('react',message=910,emoji='👍'),{'text':'Файл отправлен.'}])
            await self.message(tg,'Пришли файл и поставь реакцию',address.thread,mid=910)
            await self.until(lambda:any(m.get('text')=='Файл отправлен.' for m in self.sent(env)))
            await self.until(lambda:not tg._bg)
            document=self.sent(env,'sendDocument')[0]
            self.assertEqual(int(document['message_thread_id']),address.thread)
            reaction=self.sent(env,'setMessageReaction')[0]
            self.assertEqual(reaction['message_id'],910);self.assertNotIn('message_thread_id',reaction)
            other=app.store.new_session('chat','Другой');await tg.topics.create(app,42,other)
            result=await app.tools.run('react',{'message':910,'emoji':'👍'},ToolContext(app,session_id=other))
            self.assertFalse(result.ok)
            async def voice(*args,**kwargs):return {'audio':b'OggS-test-audio'}
            app.settings.set('voice',{'enabled':True,'telegram_reply':True})
            with patch.object(app.voice,'speak',voice):await tg._answer(address,sid,{'text':'Ответ голосом.'},agent_ctx=app)
            audio=self.sent(env,'sendVoice')[0]
            self.assertEqual(int(audio['message_thread_id']),address.thread)
            self.assertIn('42:'+str(audio['message_id']),app.store.meta_get('telegram:refs'))
            app.store.update_session(sid,title='Новый заголовок')
            await tg.topics.rename(app,sid)
            edit=self.sent(env,'editForumTopic')[-1]
            self.assertEqual(edit['message_thread_id'],address.thread);self.assertIn('Новый заголовок',edit['name'])

    @async_test
    async def test_user_created_topic_and_missing_id_require_evidence_or_choice(self):
        async with MindEnv(self.tmp) as env:
            tg=await self.up(env);app=env.app
            await self.message(tg,thread=300,forum_topic_created={'name':'План хозяина','icon_color':7322096})
            row=tg.topics.lookup(42,300);self.assertIsNotNone(row)
            self.assertIn('План хозяина',row['name']);self.assertFalse(app.mind.turns)
            tg.remember(app,Address(42,300),888,row['session'],'Прежний ответ','out')
            env.script([{'text':'По известной цитате.'}])
            await self.message(tg,'Без номера, но с известным ответом',is_topic_message=True,reply_to_message={'message_id':888,'text':'Прежний ответ'})
            await self.until(lambda:any(m.get('text')=='По известной цитате.' for m in self.sent(env)))
            self.assertEqual(next(m for m in self.sent(env) if m.get('text')=='По известной цитате.')['message_thread_id'],300)
            await self.until(lambda:not tg._bg)
            count=len([r for r in env.fake.state.requests if r.get('api')=='openai'])
            await self.message(tg,'Без номера и без цитаты',is_topic_message=True,mid=912)
            self.assertEqual(len([r for r in env.fake.state.requests if r.get('api')=='openai']),count)
            card=next(m for m in reversed(self.sent(env)) if 'Telegram не указал' in m.get('text',''))
            button=card['reply_markup']['inline_keyboard'][0][0]
            env.script([{'text':'После выбора хозяина.'}])
            await tg._callback({'id':'route','from':{'id':42},'message':{'chat':{'id':42},'message_id':card['message_id']},'data':button['callback_data']})
            await self.until(lambda:any(m.get('text')=='После выбора хозяина.' for m in self.sent(env)))
            self.assertEqual(next(m for m in self.sent(env) if m.get('text')=='После выбора хозяина.')['message_thread_id'],300)
            rows=app.store.messages(env.sid)
            self.assertTrue(rows);self.assertTrue(all(row['meta'].get('service_event') for row in rows))
            self.assertFalse(app.mind.running(env.sid))

    @async_test
    async def test_lost_topic_recreated_once_with_retry_and_agent_cannot_invoke_repair(self):
        async with MindEnv(self.tmp) as env:
            tg=await self.up(env);app=env.app
            sid=app.store.new_session('chat','Восстановление');address=await tg.topics.create(app,42,sid)
            env.fake.state.tg_missing_threads.add(address.thread)
            await tg.send(address,'После пересоздания.')
            new=tg.topics.for_session(app,sid)
            self.assertNotEqual(new.thread,address.thread)
            messages=[m for m in self.sent(env) if m.get('text')=='После пересоздания.']
            self.assertEqual(len([m for m in messages if not m.get('rejected')]),1)
            self.assertEqual(messages[-1]['message_thread_id'],new.thread)
            self.assertEqual(len(self.sent(env,'createForumTopic')),2)
            self.assertTrue(any('тема пересоздана' in row['summary'] for row in app.journal.recent(30)))
            decision=await app.guard.check(Action('telegram.topic',f'42:{new.thread}',actor='agent',tool='telegram_topic_repair'))
            self.assertFalse(decision.allowed)
            env.fake.state.tg_missing_threads.add(new.thread)
            with self.assertRaises(HumanError):await tg.send(new,'Не создавать бесконечно.')
            self.assertEqual(len(self.sent(env,'createForumTopic')),2)
            self.assertIsNone(tg.topics.lookup(99,new.thread))

    @async_test
    async def test_button_belongs_to_topic_and_closed_conversation_does_not_restart(self):
        async with MindEnv(self.tmp) as env:
            tg=await self.up(env);app=env.app
            first=app.store.new_session('chat','Первый');one=await tg.topics.create(app,42,first)
            second=app.store.new_session('chat','Второй');two=await tg.topics.create(app,42,second)
            await self.message(tg,'/chats',one.thread)
            card=self.sent(env)[-1];button=card['reply_markup']['inline_keyboard'][0][0]
            await tg._callback({'id':'other-topic','from':{'id':42},'message':{'chat':{'id':42},'message_thread_id':two.thread,'message_id':card['message_id']},'data':button['callback_data']})
            self.assertEqual(len(self.sent(env,'createForumTopic')),2)
            self.assertIn('другому разговору',self.sent(env)[-1]['text'])
            await tg._callback({'id':'own-topic','from':{'id':42},'message':{'chat':{'id':42},'message_thread_id':one.thread,'message_id':card['message_id']},'data':button['callback_data']})
            self.assertEqual(len(self.sent(env,'createForumTopic')),3)
            app.store.update_session(first,meta={'archived':True})
            before=len([r for r in env.fake.state.requests if r.get('api')=='openai'])
            await self.message(tg,'Не запускать закрытую историю',one.thread)
            self.assertIn('Разговор темы закрыт',self.sent(env)[-1]['text'])
            self.assertEqual(len([r for r in env.fake.state.requests if r.get('api')=='openai']),before)
            env.fake.state.tg_missing_threads.add(one.thread)
            with self.assertRaises(HumanError):await tg.send(one,'Не восстанавливать архив.')
            self.assertEqual(len(self.sent(env,'createForumTopic')),3)

    @async_test
    async def test_topics_off_uses_buttons_and_optional_agent_name(self):
        async with MindEnv(self.tmp) as env:
            tg=await self.up(env);app=env.app
            app.settings.set('telegram',{'topics':False,'agent_label':True})
            child=app.agents.get(app.agents.create('Сосед',awaken=False)['id']);tg.choose(42,child)
            await self.message(tg,'/new Без темы')
            self.assertFalse(self.sent(env,'createForumTopic'))
            env.script([{'text':'Здесь мой ответ.'}]);await self.message(tg,'Привет',mid=913)
            await self.until(lambda:any('Здесь мой ответ.' in m.get('text','') for m in self.sent(env)))
            result=next(m for m in self.sent(env) if 'Здесь мой ответ.' in m.get('text',''))
            from bender.agents.badge import emoji
            self.assertEqual(result['text'],emoji(child)+': Здесь мой ответ.')
            self.assertNotIn('message_thread_id',result)
            await self.message(tg,'/chats')
            card=self.sent(env)[-1];buttons=card['reply_markup']['inline_keyboard']
            self.assertEqual(buttons[0][0]['text'],'Новый чат')
