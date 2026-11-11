"""Родной JSON Gemini: аудио-вход, PCM→WAV, ключ-заголовок и запасная цепочка."""
import base64
import io
import wave
from bender.core.errors import HumanError
from bender.router.catalog import list_models
from bender.voice import gemini
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv
from tests import test_local_voice as local


class GeminiVoiceTest(TempDirCase):
    start_server=local.LocalVoiceTest.start_server
    @async_test
    async def test_native_format_auth_pcm_wav_and_transcription_taint(self):
        async with MindEnv(self.tmp) as env:
            app=env.app
            provider=app.models.add_provider(preset='gemini_voice',base_url=env.base+'/v1beta',api_key='fake-google-key',extra={'is_local':True})
            app.models.set_role('ears',[{'provider':provider['id'],'model':'gemini-3.5-transcribe'}])
            app.models.set_role('voice',[{'provider':provider['id'],'model':'gemini-3.8-flash-tts'}])
            app.settings.set('voice',{'enabled':True,'format':'wav','gemini_speaker':'Kore'})
            result=await app.voice.transcribe(b'OggS-fixture',ext='ogg',seconds=2,from_owner=False)
            self.assertTrue(result['tainted']);self.assertEqual(result['text'],'Расшифровка Gemini по-русски.')
            speech=await app.voice.speak('Привет хозяину')
            self.assertEqual(speech['mime'],'audio/wav')
            with wave.open(io.BytesIO(speech['audio'])) as decoded:
                self.assertEqual(decoded.getframerate(),24000);self.assertEqual(decoded.getnframes(),3600);self.assertEqual(decoded.getsampwidth(),2)
            requests=[entry for entry in env.fake.state.requests if entry.get('api')=='gemini_audio']
            self.assertEqual(len(requests),2);self.assertTrue(all(entry['google_key_present'] and not entry['bearer_present'] for entry in requests))
            part=requests[0]['body']['contents'][0]['parts'][1]['inlineData']
            self.assertEqual(part,{'mimeType':'audio/ogg','data':base64.b64encode(b'OggS-fixture').decode()})
            generation=requests[1]['body']['generationConfig']
            self.assertEqual(generation['responseModalities'],['AUDIO']);self.assertEqual(generation['speechConfig']['voiceConfig']['prebuiltVoiceConfig']['voiceName'],'Kore')
            with self.assertRaises(HumanError):await app.voice.speak('PCM не выдаётся за Opus',format='opus')
            catalog=await list_models(app.http,provider,'fake-google-key')
            self.assertEqual(catalog[0]['id'],'gemini-3.5-transcribe')
            self.assertIn('улучшения продуктов Google',provider['note']);self.assertIn('России',provider['note'])

    @async_test
    async def test_pcm_for_telegram_is_encoded_only_in_optional_local_process(self):
        server=self.start_server()
        async with MindEnv(self.tmp) as env:
            app=env.app
            provider=app.models.add_provider(preset='gemini_voice',base_url=env.base+'/v1beta',api_key='fake-key',extra={'is_local':True})
            app.models.set_role('voice',[{'provider':provider['id'],'model':'gemini-3.8-flash-tts'}])
            app.settings.set('voice',{'enabled':True});app.settings.set('voice_local',{'enabled':True,'port':server.server_address[1]})
            (app.dirs.local/'voice').mkdir();(app.dirs.local/'voice'/'token').write_text('own-test-key')
            result=await app.voice.speak('Голос для Telegram',format='opus')
            self.assertEqual(result['mime'],'audio/ogg');self.assertTrue(result['audio'].startswith(b'OggS'))
            self.assertEqual(server.engine.inputs[0][0],'opus');self.assertTrue(server.engine.inputs[0][1].startswith(b'RIFF'))

    @async_test
    async def test_gemini_error_falls_back_to_whisper_format(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;fallback=next(iter(app.models.providers()))
            provider=app.models.add_provider(preset='gemini_voice',base_url=env.base+'/v1beta',api_key='fake-key',extra={'is_local':True})
            app.models.set_role('ears',[{'provider':provider['id'],'model':'audio-error','params':{'audio_protocol':'gemini'}},{'provider':fallback,'model':'ears'}])
            app.settings.set('voice',{'enabled':True})
            result=await app.voice.transcribe(b'wave-fixture',ext='wav',seconds=1)
            self.assertEqual(result['model'],'ears');self.assertEqual(result['text'],'Привет голосом')
            self.assertEqual(len([entry for entry in env.fake.state.requests if entry.get('api')=='gemini_audio']),1)

    def test_bad_pcm_and_empty_candidates_are_human_errors(self):
        for raw in (b'{}',b'{"candidates":[]}',b'{"candidates":[{"content":{"parts":[{"inlineData":{"mimeType":"image/png","data":"YQ=="}}]}}]}'):
            with self.assertRaises(HumanError):gemini.response(raw,'voice')
