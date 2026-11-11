"""Звук в модели: выбранный кандидат, разумный размер окна и доверие каналов."""
import base64
import json
from types import SimpleNamespace
from bender.channels import media
from bender.mind.context import message_tokens
from bender.net.http_server import HttpServer
from tests.helpers import owner_web as create_web, owner_headers
from bender.voice.input import prepare,attachment
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv
from tests import test_telegram as telegram
from tests import test_local_voice as local

wav=local.wav


class NativeAudioTest(TempDirCase):
    start_server=local.LocalVoiceTest.start_server
    _up=telegram.TelegramTest._up
    until=telegram.TelegramTest.until
    sent=telegram.TelegramTest.sent

    def configure(self,app,formats=None):
        provider=next(iter(app.models.providers()))
        app.models.update_provider(provider,{'models':[{'id':'script','audio_input':True,'audio_formats':formats or ['wav']}]})
        app.settings.set('voice',{'enabled':True,'stt_mode':'native'})
        return provider

    def test_audio_estimate_ignores_base64_and_candidate_filter_is_independent_of_vision(self):
        part={'type':'audio','format':'wav','data':'A'*1000000,'seconds':2}
        self.assertLess(message_tokens({'content':[part]}),1000)
        candidate=SimpleNamespace(provider={'kind':'openai','models':[{'id':'look','vision':True,'audio_input':False}]},model='look')
        messages=[{'role':'user','content':[{'type':'image','data':'x','media_type':'image/png'},part]}]
        adapted=media.for_model(messages,candidate)[0]['content']
        self.assertEqual(adapted[0]['type'],'image');self.assertEqual(adapted[1]['type'],'text')
        self.assertIn('не принимает',adapted[1]['text'])
        candidate.provider['models'][0].update(audio_input=True,audio_formats=['wav'])
        self.assertEqual(media.for_model(messages,candidate)[0]['content'][1],part)
        self.assertEqual(media.for_model(messages,candidate,audio=False)[0]['content'][1]['type'],'text')

    @async_test
    async def test_agent_cannot_enable_raw_audio_without_owner_even_guard_off(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;app.settings.set('voice',{'enabled':True});app.settings.set('guard',{'enabled':False});env.auto_approve('deny')
            from bender.tools.registry import ToolContext
            result=await app.tools.run('settings',{'group':'voice','changes':{'stt_mode':'native'}},ToolContext(app,session_id=env.sid))
            self.assertFalse(result.ok);self.assertEqual(app.settings.get('voice','stt_mode'),'transcribe')
            self.assertEqual(len(env.approvals),1)

    @async_test
    async def test_native_and_both_send_input_audio_with_own_transcript(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;provider=self.configure(app)
            native=await prepare(app,wav(),ext='wav',seconds=1,session=env.sid)
            self.assertTrue(native['native']);self.assertEqual(native['text'],'')
            item=await attachment(app,native,session=env.sid)
            env.script([{'text':'Ответ по звуку.'}])
            await app.mind.say(env.sid,'Голосовое.',content=media.content('Голосовое.',[item],audio=True),tainted=True)
            request=next(r for r in env.fake.state.requests if r.get('api')=='openai')
            parts=next(m for m in reversed(request['body']['messages']) if m['role']=='user')['content']
            sound=next(part['input_audio'] for part in parts if part['type']=='input_audio')
            self.assertEqual(sound['format'],'wav');self.assertEqual(base64.b64decode(sound['data']),wav())
            child=app.agents.get(app.agents.create('Сосед',awaken=False)['id'])
            child.settings.set('voice',{'enabled':True,'stt_mode':'both'})
            child.models.set_role('ears',[{'provider':provider,'model':'ears'}])
            both=await prepare(child,wav(),ext='wav',seconds=1,session=child.main_session(),from_owner=False)
            self.assertEqual(both['text'],'Привет голосом');self.assertTrue(both['tainted']);self.assertTrue(both['native'])
            self.assertEqual(app.settings.get('voice','stt_mode'),'native')
            self.assertEqual(app.models.roles().get('ears',[]),[])

    @async_test
    async def test_incompatible_format_uses_transcription_instead_of_fake_hearing(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;provider=self.configure(app)
            app.models.set_role('ears',[{'provider':provider,'model':'ears'}])
            result=await prepare(app,b'OggS-fixture',ext='ogg',seconds=1,session=env.sid)
            self.assertFalse(result['native']);self.assertEqual(result['text'],'Привет голосом')
            self.assertIn('использована расшифровка',result['notice'])

    @async_test
    async def test_forwarded_telegram_native_audio_stays_tainted_and_never_command(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;self.configure(app,['ogg']);await self._up(env,owner=True)
            env.script([{'text':'Ответ на запись.'}])
            await app.telegram._handle({'message':{'message_id':721,'chat':{'id':42,'type':'private'},'from':{'id':42},'voice':{'file_id':'voice','duration':1},'caption':'/stop','forward_origin':{'type':'hidden_user'}}})
            await self.until(lambda:any(m.get('text')=='Ответ на запись.' for m in self.sent(env)))
            await self.until(lambda:not app.telegram._bg)
            self.assertFalse(app.guard.halted)
            user=next(row for row in app.store.messages(env.sid) if row['role']=='user')
            self.assertTrue(user['meta']['untrusted']);self.assertTrue(any(part.get('type')=='audio' for part in user['content']))
            self.assertIn('Подпись к голосовому: /stop',user['content'][0]['text'])
            self.assertFalse(any(row['role']=='ears' for row in app.store.q('SELECT role FROM calls')))

    @async_test
    async def test_ogg_is_normalized_in_optional_process_and_original_is_kept(self):
        server=self.start_server()
        async with MindEnv(self.tmp) as env:
            app=env.app;self.configure(app)
            app.settings.set('voice_local',{'enabled':True,'port':server.server_address[1]})
            (app.dirs.local/'voice').mkdir();(app.dirs.local/'voice'/'token').write_text('own-test-key')
            raw=b'OggS-owner-original'
            original=await media.save(app,'original.ogg',raw,kind='voice',source='telegram',session=env.sid)
            result=await prepare(app,raw,ext='ogg',seconds=1,session=env.sid)
            self.assertTrue(result['native']);self.assertEqual(result['ext'],'wav')
            self.assertEqual(server.engine.inputs,[('normalize',raw)])
            native=await attachment(app,result,session=env.sid,existing=original,source='telegram')
            from pathlib import Path
            self.assertEqual(Path(original['path']).read_bytes(),raw)
            self.assertNotEqual(native['id'],original['id']);self.assertTrue(Path(native['path']).read_bytes().startswith(b'RIFF'))

    @async_test
    async def test_pult_prepares_attachment_and_sends_only_in_selected_context(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;self.configure(app)
            server=HttpServer(create_web(app,allowed_hosts={'127.0.0.1'}));await server.start('127.0.0.1',0)
            url='http://127.0.0.1:'+str(server.port)
            try:
                response=await app.http.post(url+'/api/voice/transcribe',headers=owner_headers(**{'X-Bender':'1'}),json={'audio':base64.b64encode(wav()).decode(),'ext':'wav','seconds':1,'session':env.sid})
                async with response:prepared=json.loads(await response.read())
                self.assertEqual(response.status,200,prepared);self.assertTrue(prepared['native']);self.assertIn('attachment',prepared)
                env.script([{'text':'Пульт слышит звук.'}])
                response=await app.http.post(url+'/api/say',headers=owner_headers(**{'X-Bender':'1'}),json={'text':'Запись хозяина','session':env.sid,'attachments':[prepared['attachment']['id']]})
                async with response:await response.read()
                await self.until(lambda:not app.mind.running(env.sid))
                user=next(row for row in app.store.messages(env.sid) if row['role']=='user')
                self.assertTrue(user['meta']['untrusted']);self.assertTrue(any(part.get('type')=='audio' for part in user['content']))
                child=app.agents.create('Другой',awaken=False)
                response=await app.http.get(url+'/api/attachments/'+prepared['attachment']['id'],headers=owner_headers(**{'X-Bender-Agent':child['id']}))
                async with response:await response.read()
                self.assertEqual(response.status,404)
            finally:await server.stop()
