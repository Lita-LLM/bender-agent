"""Аудиоцепочки, счёт, выключатель и доверие пересланной речи."""
import asyncio
from pathlib import Path
from unittest.mock import patch
from bender.core.errors import HumanError
from bender.channels.telegram import OWNER_META,TOKEN_SECRET
from bender.mind.loop import Turn
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv


class VoiceTest(TempDirCase):
    def configure(self,env):
        app=env.app
        provider=next(iter(app.models.providers()))
        app.models.set_role('ears',[{'provider':provider,'model':'audio-error'},{'provider':provider,'model':'ears'}])
        app.models.set_role('voice',[{'provider':provider,'model':'voice'}])
        app.settings.set('voice',{'enabled':True,'stt_usd_minute':.06,'tts_usd_million_chars':1000})

    @async_test
    async def test_chain_billing_limits_and_off(self):
        async with MindEnv(self.tmp) as env:
            self.configure(env)
            with patch.object(env.app.router,'is_free',return_value=False):
                result=await env.app.voice.transcribe(b'webm fixture',seconds=60,from_owner=False,session_id=env.sid)
                self.assertEqual(result['text'],'Привет голосом')
                self.assertTrue(result['tainted'])
                self.assertAlmostEqual(result['cost'],.06)
                speech=await env.app.voice.speak('1234567890',session_id=env.sid,format='opus')
                self.assertTrue(speech['audio'].startswith(b'OggS'))
                self.assertIn(b'OpusHead',speech['audio'])
                self.assertAlmostEqual(speech['cost'],.01)
            self.assertAlmostEqual(env.app.cost_today(),.07)
            self.assertGreaterEqual(env.app.store.one('SELECT count(*) FROM calls')[0],3)
            with self.assertRaises(HumanError):await env.app.voice.transcribe(b'x',seconds=float('nan'))
            env.app.settings.set('voice',{'enabled':False})
            with self.assertRaises(HumanError):await env.app.voice.speak('нет')

    @async_test
    async def test_telegram_forwarded_voice_and_native_opus_reply(self):
        async with MindEnv(self.tmp) as env:
            self.configure(env)
            app=env.app
            app.settings.set('telegram',{'api_base':env.base+'/tg'})
            app.secrets.set(TOKEN_SECRET,env.fake.state.tg_token)
            app.store.meta_set(OWNER_META,{'user_id':42,'chat_id':42})
            app.settings.set('telegram', {'enabled': True})
            app.telegram.start()
            for _ in range(100):
                if app.telegram.state == 'ok': break
                await asyncio.sleep(.02)
            env.script([{'text':'Ответ голосом'}])
            app.settings.set('voice',{'telegram_reply':True})
            msg={'message_id':1,'from':{'id':42},'chat':{'id':42,'type':'private'},'voice':{'file_id':'test','duration':2},'forward_origin':{'type':'user'}}
            await app.telegram._handle({'message':msg})
            for _ in range(100):
                if any(r['method']=='sendVoice' for r in env.fake.state.tg_sent):break
                await asyncio.sleep(.02)
            self.assertTrue(any(r['method']=='sendVoice' for r in env.fake.state.tg_sent))
            rows=app.store.messages(env.sid)
            user=next(r for r in rows if r['role']=='user')
            self.assertTrue(user['meta']['untrusted'])
            self.assertTrue(app.store.session(env.sid)['meta']['last_turn']['tainted'])
            calls=app.store.one('SELECT COUNT(*) FROM calls')[0]
            msg['from']={'id':99}
            await app.telegram._handle({'message':msg})
            self.assertEqual(app.store.one('SELECT COUNT(*) FROM calls')[0],calls)

    @async_test
    async def test_queued_forward_keeps_taint_and_not_a_command(self):
        async with MindEnv(self.tmp) as env:
            turn=Turn(env.sid,'telegram','owner',False)
            turn.task=asyncio.create_task(asyncio.sleep(10))
            env.app.mind.turns[env.sid]=turn
            await env.app.mind.say(env.sid,'/научись чужой текст',channel='telegram',actor='owner',tainted=True)
            self.assertTrue(turn.inbox[0][1]['untrusted'])
            env.app.mind._flush_inbox(turn)
            self.assertTrue(turn.tainted)
            turn.task.cancel()
            try:await turn.task
            except asyncio.CancelledError:pass

    @async_test
    async def test_native_local_protocols(self):
        async with MindEnv(self.tmp) as env:
            self.configure(env)
            app=env.app
            stt=app.models.add_provider(name='whisper.cpp',base_url=env.base,extra={'is_local':True})
            tts=app.models.add_provider(name='Piper',base_url=env.base+'/piper',extra={'is_local':True})
            app.models.set_role('ears',[{'provider':stt['id'],'model':'local'}])
            app.models.set_role('voice',[{'provider':tts['id'],'model':'local'}])
            app.settings.set('voice',{'stt_protocol':'whisper','tts_protocol':'piper'})
            result=await app.voice.transcribe(b'wave fixture',ext='wav',seconds=1)
            self.assertFalse(result['tainted'])
            speech=await app.voice.speak('местный голос')
            self.assertEqual(speech['mime'],'audio/wav')
            self.assertTrue(speech['audio'].startswith(b'RIFF'))
            with self.assertRaises(HumanError):await app.voice.speak('Telegram',format='opus')
