"""Часть 6: номера/цитаты, промежуточные сообщения, реакции без пробуждения."""
import asyncio
import re
import unittest
from pathlib import Path
from bender.channels.reactions import EMOJI, emoji
from bender.core.errors import HumanError
from bender.core.dates import content
from bender.tools.registry import ToolContext
from bender.workshop.metrics import snapshot
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call
from tests import test_telegram as original


class ReactionListTest(unittest.TestCase):
    def test_all_codepoints_match_saved_reference(self):
        self.assertEqual(len(EMOJI),73)
        path=Path(__file__).resolve().parents[1]/'docs/research/telegram-bot-api-10.3.md'
        if path.exists():  # выдержка документации есть только в мастерской, в выпуск не входит
            points=re.findall(r'^(U\+[A-F0-9]+(?: U\+[A-F0-9]+)*)  ',path.read_text(),re.M)
            self.assertEqual(EMOJI,tuple(''.join(chr(int(n[2:],16)) for n in p.split()) for p in points))
        self.assertEqual(emoji('❤\ufe0f'),'❤')
        self.assertEqual(emoji('🤷\u200d♂\ufe0f'),'🤷\u200d♂')
        with self.assertRaises(HumanError):emoji('🦆')


class ConversationTest(TempDirCase):
    _up=original.TelegramTest._up
    push=original.TelegramTest.push
    sent=original.TelegramTest.sent
    until=original.TelegramTest.until

    @async_test
    async def test_send_react_and_one_final_with_numbered_history(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True)
            env.script([call('send_message',text='Смотрю…',reply_to=1001),call('react',message=1001,emoji='❤\ufe0f'),{'text':'Готово.'}])
            self.push(env,'Посмотри')
            await self.until(lambda:len(self.sent(env))==2)
            first,last=self.sent(env)
            self.assertEqual(first['reply_parameters']['message_id'],1001)
            self.assertEqual(last['text'],'Готово.')
            self.assertNotIn('reply_parameters',last)
            reaction=self.sent(env,'setMessageReaction')[0]
            self.assertEqual(reaction['reaction'],[{'type':'emoji','emoji':'❤'}])
            await self.until(lambda:not env.app.telegram._bg)
            rows=env.app.store.messages(env.sid)
            self.assertIn('[#1001]',content(rows[0]))
            self.assertIn('[#'+str(last['message_id'])+']',content(next(r for r in reversed(rows) if r['role']=='assistant')))

    @async_test
    async def test_sent_all_prevents_duplicate_and_unknown_number_is_denied(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True)
            env.script([call('send_message',text='Всё готово.',complete=True),{'text':'Всё готово.'}])
            self.push(env,'Сделай')
            await self.until(lambda: self.sent(env) and not env.app.mind.running(env.sid))
            await asyncio.sleep(.05)
            self.assertEqual(len(self.sent(env)),1)
            result=await env.app.tools.run('react',{'message':7654321,'emoji':'👍'},ToolContext(env.app,session_id=env.sid))
            self.assertFalse(result.ok)
            self.assertEqual(self.sent(env,'setMessageReaction'),[])
            env.script([{'text':'Теперь в пульте.'}])
            await env.app.mind.say(env.sid,'Пишу из пульта',channel='пульт')
            self.assertEqual(env.app.store.session(env.sid)['meta']['home_channel'],'pult')

    @async_test
    async def test_reactions_queue_until_next_turn_and_change_rating(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True)
            app=env.app;tg=app.telegram
            tg.remember(app,42,4810,env.sid,'Ответ','out')
            event={'chat':{'id':42},'user':{'id':42},'message_id':4810,'old_reaction':[], 'new_reaction':[{'type':'emoji','emoji':'👍'}]}
            before=len(env.fake.state.requests)
            await tg._handle({'message_reaction':event})
            self.assertEqual(len(env.fake.state.requests),before)
            self.assertFalse(app.mind.running(env.sid))
            self.assertEqual(snapshot(app)['owner_reactions'],{'👍':1,'👎':0})
            env.script([{'text':'Увидел.'}]);self.push(env,'Дальше')
            await self.until(lambda:self.sent(env))
            self.assertIn('Хозяин поставил 👍 на #4810',app.store.messages(env.sid)[0]['content'])
            self.assertEqual(app.store.meta_get('telegram:reactions:'+env.sid),[])
            event.update(old_reaction=event['new_reaction'],new_reaction=[{'type':'emoji','emoji':'👎'}])
            await tg._handle({'message_reaction':event})
            self.assertEqual(snapshot(app)['owner_reactions'],{'👍':0,'👎':1})
            await tg._handle({'message_reaction':{**event,'user':{'id':99}}})
            self.assertEqual(len(app.store.meta_get('telegram:reactions:'+env.sid)),1)

    @async_test
    async def test_reply_to_notice_or_rich_answer_brings_its_text(self):
        # Богатые сообщения и уведомления Telegram цитирует без текста — агент берёт его из памяти отправленного.
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True)
            env.app.bus.publish({'type':'notify','kind':'pulse','text':'💓 Сделал сам: проверил чат, новых вопросов нет'})
            await self.until(lambda:env.fake.state.tg_sent)
            mid=env.fake.state.tg_sent[-1]['message_id']
            self.assertIn('новых вопросов нет',str(env.fake.state.tg_sent[-1]))
            env.script([{'text':'Да, это был пульс.'}])
            await env.app.telegram._handle({'message':{'message_id':1001,'from':{'id':42},'chat':{'id':42,'type':'private'},'text':'Что-то вы ничего не делаете',
                'reply_to_message':{'message_id':mid}}})
            await self.until(lambda:env.app.store.messages(env.sid))
            said=[m['content'] for m in env.app.store.messages(env.sid) if m['role']=='user'][-1]
            self.assertIn('проверил чат, новых вопросов нет',str(said));self.assertNotIn('медиа',str(said))
            # Уведомление соседа по стае: ответ идёт выбранному агенту, но с текстом и именем автора.
            env.app.agents.create('Луна',agent_id='luna',awaken=False)
            env.app.agents.get('luna').bus.publish({'type':'notify','kind':'pulse','text':'💓 Сделал сам: чат проверила'})
            mine=lambda word:[e['message_id'] for e in env.fake.state.tg_sent if word in str(e)]
            await self.until(lambda:mine('чат проверила'))
            other=mine('чат проверила')[0]
            env.script([{'text':'Это Луна.'}])
            await env.app.telegram._handle({'message':{'message_id':1003,'from':{'id':42},'chat':{'id':42,'type':'private'},'text':'А это?',
                'reply_to_message':{'message_id':other}}})
            await self.until(lambda:'А это?' in str([m['content'] for m in env.app.store.messages(env.sid)]))
            said=[m['content'] for m in env.app.store.messages(env.sid) if m['role']=='user'][-1]
            self.assertIn('[Луна] 💓 Сделал сам: чат проверила',str(said))

    @async_test
    async def test_owner_quote_and_explicit_final_reply(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True)
            env.app.telegram.remember(env.app,42,4812,env.sid,'Прежний ответ','out')
            env.script([{'text':'↩#4812 Да.'}])
            await env.app.telegram._handle({'message':{'message_id':1001,'from':{'id':42},'chat':{'id':42,'type':'private'},'text':'Уточни',
                'reply_to_message':{'message_id':4812,'text':'Прежний ответ'}}})
            await self.until(lambda:self.sent(env))
            self.assertEqual(self.sent(env)[0]['reply_parameters']['message_id'],4812)
            self.assertEqual(self.sent(env)[0]['text'],'Да.')
            self.assertIn('Прежний ответ',env.app.store.messages(env.sid)[0]['content'])
