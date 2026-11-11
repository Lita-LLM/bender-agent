"""Поток через настоящий фальшивый Bot API, ограничение частоты и запасной путь."""
import asyncio
import time
from unittest.mock import patch

from bender.channels.telegram import TelegramError
from bender.channels.telegram_live import Live
from bender.tools.registry import ToolResult
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call
import tests.test_telegram as telegram_tests


class TelegramLiveTest(TempDirCase):
    _up=telegram_tests.TelegramTest._up
    until=telegram_tests.TelegramTest.until
    sent=telegram_tests.TelegramTest.sent

    @async_test
    async def test_one_draft_throttle_thoughts_intermediate_tool_safety_and_one_final(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('telegram',{'live_via':'draft'})  # проверяется именно черновик Telegram
            app=env.app;await self._up(env,owner=True)
            app.secrets.set('private-test','fake-thought-secret')
            times=[];original=app.telegram.api
            async def tracked(method,payload=None,**kwargs):
                if method=='sendMessageDraft': times.append(time.monotonic())
                return await original(method,payload,**kwargs)
            async def tool(ctx,items):
                await asyncio.sleep(1.05)
                return ToolResult('EVIL-RAW-TOOL-OUTPUT',summary='план проверен',tainted=True)
            env.script([{'reasoning':'Проверю условия\nfake-thought-secret https://example.org/?key=foo', 'text':'Промежуточная реплика', **call('plan',items=[])},
                        {'text':'Это окончательный ответ хозяину без промежуточной реплики.', 'delay':.2}])
            with patch.object(app.telegram,'api',tracked),patch.object(app.tools.tools['plan'],'handler',tool):
                await app.mind.say(env.sid,'работай',channel='telegram')
                await self.until(lambda:len(self.sent(env))==1)
                await self.until(lambda:not app.telegram._bg)
            drafts=self.sent(env,'sendMessageDraft')
            self.assertGreaterEqual(len(drafts),2)
            self.assertEqual(len({r['draft_id'] for r in drafts}),1);self.assertNotEqual(drafts[0]['draft_id'],0)
            self.assertTrue(all(b-a>=.7 for a,b in zip(times,times[1:])),times)
            joined='\n'.join(r['text'] for r in drafts)
            self.assertIn('<i>',joined);self.assertIn('Промежуточная',joined)
            self.assertNotIn('fake-thought-secret',joined);self.assertNotIn('example.org',joined)
            self.assertNotIn('EVIL-RAW',joined)
            self.assertEqual(self.sent(env)[0]['text'],'Это окончательный ответ хозяину без промежуточной реплики.')
            self.assertFalse(app.telegram._live)
            self.assertFalse(any('can_stop' in r for r in drafts), 'кнопка Стоп черновика не просится: она мешала писать агенту')

    @async_test
    async def test_default_status_message_leaves_input_open_and_message_steers_turn(self):
        """По умолчанию — не черновик (он закрывает хозяину отправку), а статус-сообщение; написанное во время хода
        встаёт в тот же ход без прерывания."""
        async with MindEnv(self.tmp) as env:
            app=env.app;await self._up(env,owner=True);tg=app.telegram
            from bender.core.settings import SCHEMA
            self.assertEqual(SCHEMA['telegram'].field('live_via').default,'message')
            app.settings.set('telegram',{'live_via':'message'})  # _up общих тестов ставит draft
            entered=asyncio.Event();release=asyncio.Event()
            @app.tools.tool('live_wait','Подождать',group='base')
            async def waiting(ctx):entered.set();await release.wait();return ToolResult('Закончил ожидание')
            env.script([call('live_wait'),{'text':'Учёл: три.'}])
            def owner(mid,text):return {'message':{'message_id':mid,'from':{'id':42},'chat':{'id':42,'type':'private'},'text':text}}
            with patch('bender.channels.telegram_live.FALLBACK_INTERVAL',.03):
                await tg._handle(owner(1001,'Купи два'))
                await asyncio.wait_for(entered.wait(),3)
                await self.until(lambda:bool(self.sent(env)))
                await self.until(lambda:any('live_wait' in str(s.get('text')) for s in self.sent(env,'editMessageText')))  # статус правится на месте
                await tg._handle(owner(1002,'Нет, три'))
                await self.until(lambda:any(s.get('message_id')==1002 for s in self.sent(env,'setMessageReaction')))
                self.assertTrue(app.mind.running(env.sid))  # ход не прерван
                release.set()
                await self.until(lambda:any(str(s.get('text','')).endswith('Учёл: три.') for s in self.sent(env)))
                await self.until(lambda:bool(self.sent(env,'deleteMessage')))
            self.assertFalse(self.sent(env,'sendMessageDraft'));self.assertFalse(self.sent(env,'sendRichMessageDraft'))
            self.assertEqual(len(self.sent(env,'deleteMessage')),1)  # статус убран, остался только ответ
            self.assertEqual(self.sent(env,'deleteMessage')[0]['message_id'],self.sent(env)[0]['message_id'])
            rows=app.store.messages(env.sid)
            self.assertEqual([r['role'] for r in rows],['user','assistant','tool','user','assistant'])
            self.assertEqual(rows[3]['content'],'Нет, три')
            self.assertEqual(len([r for r in env.fake.state.requests if r.get('api')=='openai']),2)  # второй шаг того же хода

    @async_test
    async def test_keepalive_same_draft_and_text_only(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('telegram',{'live_via':'draft'})  # проверяется именно черновик Telegram
            app=env.app;await self._up(env,owner=True)
            app.settings.set('telegram',{'live':'text'},by='agent')
            with patch('bender.channels.telegram_live.DRAFT_INTERVAL',.02),patch('bender.channels.telegram_live.KEEPALIVE',.05):
                live=Live(app.telegram,42,env.sid,123)
                live.feed({'type':'delta','kind':'reasoning','text':'Мысли не показывать'})
                live.feed({'type':'delta','kind':'text','text':'Текст'})
                await self.until(lambda:len(self.sent(env,'sendMessageDraft'))>=3)
                await live.finish()
            drafts=self.sent(env,'sendMessageDraft')
            self.assertEqual({d['draft_id'] for d in drafts},{123})
            self.assertEqual({d['text'] for d in drafts},{'Текст'})
            self.assertTrue(all('Думаю' not in d['text'] for d in drafts))

    @async_test
    async def test_method_not_found_one_editable_status_deleted_then_final(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('telegram',{'live_via':'draft'})  # проверяется именно черновик Telegram
            app=env.app;await self._up(env,owner=True);original=app.telegram.api;attempts=[]
            async def old_api(method,payload=None,**kwargs):
                attempts.append((method,dict(payload or {})))
                if method=='sendMessageDraft':raise TelegramError('tg_api','Telegram: method not found','sendMessageDraft',data={'code':404})
                return await original(method,payload,**kwargs)
            with patch.object(app.telegram,'api',old_api),patch('bender.channels.telegram_live.DRAFT_INTERVAL',.01),patch('bender.channels.telegram_live.FALLBACK_INTERVAL',.03):
                app.store.update_session(env.sid,meta={'home_channel':'telegram'})
                live=Live(app.telegram,42,env.sid,456)
                app.telegram._live[env.sid]=live
                await self.until(lambda:bool(self.sent(env)))
                live.feed({'type':'tool_end','name':'read_file','ok':True,'summary':'прочитан файл','content':'RAW-UNTRUSTED'})
                await self.until(lambda:bool(self.sent(env,'editMessageText')))
                await app.telegram._finish_answer(app.telegram._live.pop(env.sid),42,{'session':env.sid,'result':{'text':'Финал'}},None,True)
            self.assertEqual(len(self.sent(env)),2)  # временный статус + один финал
            self.assertEqual(self.sent(env)[-1]['text'],'Финал')
            self.assertEqual(len(self.sent(env,'deleteMessage')),1)
            self.assertNotIn('RAW-UNTRUSTED',str(attempts))
            self.assertEqual(len([m for m,p in attempts if m=='sendMessageDraft']),1)

    @async_test
    async def test_429_defers_updates_and_final_stop_button_checks_chat_and_id(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set('telegram',{'live_via':'draft'})  # проверяется именно черновик Telegram
            app=env.app;await self._up(env,owner=True);original=app.telegram.api;times=[]
            async def limited(method,payload=None,**kwargs):
                if method=='sendMessageDraft':
                    times.append(time.monotonic())
                    if len(times)==1:raise TelegramError('tg_api','Telegram: Too Many Requests','sendMessageDraft',data={'code':429,'retry_after':1})
                return await original(method,payload,**kwargs)
            with patch.object(app.telegram,'api',limited):
                live=Live(app.telegram,42,env.sid,789);app.telegram._live[env.sid]=live
                await self.until(lambda:len(times)>=2,timeout=4)
                self.assertGreaterEqual(times[1]-times[0],.99)
                await app.telegram._handle({'stopped_message_generation':{'chat':{'id':99},'draft_id':'789'}})
                self.assertFalse(app.guard.halted)
                await app.telegram._handle({'stopped_message_generation':{'chat':{'id':42},'draft_id':'other'}})
                self.assertFalse(app.guard.halted)
                with patch.object(app.mind,'stop') as stop:
                    await app.telegram._handle({'stopped_message_generation':{'chat':{'id':42},'draft_id':'789'}})
                stop.assert_called_once_with(env.sid)
                self.assertFalse(app.guard.halted, 'старая кнопка прерывает ход, но не включает стоп-кран')
                await live.finish();app.telegram._live.clear()

    @async_test
    async def test_off_and_secret_prefix_not_exposed(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;await self._up(env,owner=True)
            app.secrets.set('stream-test','fake-prefix-sensitive')
            live=Live(app.telegram,42,env.sid,999)
            live.feed({'type':'delta','kind':'reasoning','text':'fake-prefix'})
            self.assertNotIn('fake-prefix',live.render())
            await live.finish()
            app.settings.set('telegram',{'live':'off'},by='agent')
            env.script([{'text':'Ответ без черновика'}])
            await app.telegram._owner_text(42,1,'текст')
            await self.until(lambda:bool(self.sent(env)))
            self.assertEqual(len(self.sent(env,'sendMessageDraft')),0)
