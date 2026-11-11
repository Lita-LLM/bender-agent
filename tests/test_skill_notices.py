"""Уведомления Telegram/терминала и единая команда удаления навыка хозяином."""
import asyncio
import io
from bender.channels.telegram import OWNER_META,TOKEN_SECRET
from bender.channels.terminal import TerminalChat
from bender.core.termui import Term
from bender.skills.experience import learn,stats
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv


class NoticeTest(TempDirCase):
    @async_test
    async def test_telegram_button_and_owner_command(self):
        async with MindEnv(self.tmp) as env:
            app=env.app
            app.settings.set('telegram',{'api_base':env.base+'/tg'})
            app.secrets.set(TOKEN_SECRET,env.fake.state.tg_token)
            app.store.meta_set(OWNER_META,{'user_id':42,'chat_id':42})
            app.telegram.state='ok'
            name='n'*64
            result=await learn(app.skills,{'name':name,'description':'Памятка проверки','body':'Проверь суммы и покажи вывод.'},ToolContext(app))
            await app.telegram._skill_notice(42,{'kind':'skill','content':'💾 Навык: '+name,'data':result})
            sent=env.fake.state.tg_sent[-1]
            callback=sent['reply_markup']['inline_keyboard'][0][0]['callback_data']
            self.assertLessEqual(len(callback.encode()),64)
            await app.telegram._callback({'id':'stranger','from':{'id':99},'data':callback})
            self.assertFalse(stats(app.skills,name).get('archived'))
            await app.telegram._callback({'id':'owner','from':{'id':42},'data':callback,'message':{'chat':{'id':42},'message_id':sent['message_id']}})
            self.assertTrue(stats(app.skills,name)['archived'])
            result=await learn(app.skills,{'name':'terminal','description':'Команда терминала','body':'Проверь источник и сопоставь результат.'},ToolContext(app))
            output=io.StringIO();terminal=TerminalChat(app,Term(output,animations=False))
            terminal.on_event({'type':'message','session':env.sid,'message':{'role':'event','kind':'skill','content':'💾 Навык: terminal','data':result}})
            self.assertIn('/убрать-навык terminal',output.getvalue())
            await app.mind.say(env.sid,'/убрать-навык terminal',channel='terminal',actor='owner')
            self.assertTrue(stats(app.skills,'terminal')['archived'])
