"""Необязательный процесс: хеши, границы установки, HTTP, роли и запасной путь."""
import os
import asyncio
import hashlib
import http.client
import io
import json
from pathlib import Path
import threading
import wave
from unittest.mock import patch
from bender.core.app import App
from bender.core.errors import HumanError
from bender.guard.policy import Action
from bender.voice import install as package
from bender.voice.local_server import Server
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv


def wav():
    out=io.BytesIO()
    with wave.open(out,'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000);stream.writeframes(b'\0\0'*16000)
    return out.getvalue()


class FakeEngine:
    def __init__(self):self.inputs=[];self.voices=[];self.speeds=[]
    def transcribe(self,raw):self.inputs.append(raw);return {'text':'Проверка русского голоса.'}
    def normalize(self,raw):self.inputs.append(('normalize',raw));return wav()
    def opus(self,raw):self.inputs.append(('opus',raw));return b'OggS'+b'\0'*12+b'OpusHead'
    def speak(self,text,fmt,voice=None,speed=1):
        self.voices.append(voice);self.speeds.append(speed)
        if fmt not in ('wav','opus'):raise ValueError('Нужен WAV/Opus')
        return (b'OggS'+b'\0'*12+b'OpusHead' if fmt=='opus' else wav()),'audio/ogg' if fmt=='opus' else 'audio/wav'


class LocalVoiceTest(TempDirCase):
    def start_server(self):
        server=Server(('127.0.0.1',0),FakeEngine(),'own-test-key')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(5)))
        return server

    def test_http_auth_origin_formats_and_shutdown_without_poll(self):
        server=self.start_server()
        def request(path,body=None,headers=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
            if isinstance(body,str):body=body.encode()
            connection.request('POST' if body is not None else 'GET',path,body,headers or {})
            response=connection.getresponse();result=(response.status,response.read());connection.close();return result
        self.assertEqual(request('/health')[0],403)
        headers={'Authorization':'Bearer own-test-key'}
        self.assertEqual(request('/health',headers={**headers,'Origin':'https://foreign.test'})[0],403)
        self.assertEqual(request('/health',headers={**headers,'Host':'foreign.test'})[0],403)
        self.assertTrue(json.loads(request('/health',headers=headers)[1])['ok'])
        payload=json.dumps({'input':'Проверка','response_format':'wav'})
        status,raw=request('/v1/audio/speech',payload,{**headers,'Content-Type':'application/json'})
        self.assertEqual(status,200);self.assertTrue(raw.startswith(b'RIFF'))
        self.assertEqual(request('/v1/audio/speech','{"input":"текст","response_format":"mp3"}',headers)[0],400)
        self.assertEqual(request('/shutdown','{}',headers)[0],200)
        self.assertTrue(server.finished.wait(2))

    def test_secrets_reload_after_owner_cli_preserves_other_keys(self):
        from bender.core.secrets import Secrets
        path=self.tmp/'secrets.json';running=Secrets(path);running.set('first','old-value')
        cli=Secrets(path);self.assertEqual(cli.get('first'),'old-value')
        running.set('second','other-key')
        cli.set('voice-key','voice-token')
        self.assertEqual(running.get('voice-key'),'voice-token')
        self.assertEqual(cli.get('second'),'other-key')
        if os.name=='posix':self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_download_hash_failure_keeps_old_file_and_removes_partial(self):
        target=self.tmp/'model.onnx';target.write_bytes(b'previous')
        item={'url':'https://fixture.invalid/model','algorithm':'sha256','hash':hashlib.sha256(b'expected').hexdigest()}
        with patch.object(package.urllib.request,'urlopen',return_value=io.BytesIO(b'wrong')):
            with self.assertRaises(HumanError):package.download(item,target)
        self.assertEqual(target.read_bytes(),b'previous');self.assertFalse(target.with_name('model.onnx.download').exists())
        value=b'new model';item.update(algorithm='git-sha1',hash=hashlib.sha1(('blob '+str(len(value))+'\0').encode()+value).hexdigest())
        with patch.object(package.urllib.request,'urlopen',return_value=io.BytesIO(value)):package.download(item,target)
        self.assertEqual(target.read_bytes(),value)

    @async_test
    async def test_install_fake_models_has_verified_files_own_roles_and_owner_only(self):
        app=App(self.tmp/'data')
        try:
            child=app.agents.get(app.agents.create('Сосед',awaken=False)['id'])
            contents={'config.json':b'{"model_type":"gigaam-v3-e2e-ctc"}','v3_e2e_ctc_vocab.txt':b'abc','v3_e2e_ctc.int8.onnx':b'ctc-model',
                      'ru_RU-irina-medium.onnx':b'piper-model','ru_RU-irina-medium.onnx.json':b'{}'}
            def info(repo):
                names=list(contents)[:3] if repo==package.REPOS['asr'] else ['ru/ru_RU/irina/medium/'+name for name in list(contents)[3:]]
                return {'sha':'a'*40,'siblings':[{'rfilename':name,'lfs':{'sha256':hashlib.sha256(contents[Path(name).name]).hexdigest()}} for name in names]}
            def create(path):Path(path).mkdir(parents=True)
            with patch.object(package,'metadata',side_effect=info),patch.object(package.platform,'machine',return_value='aarch64'),patch.object(package,'command') as command,patch.object(package.venv.EnvBuilder,'create',side_effect=create),patch.object(package.urllib.request,'urlopen',side_effect=lambda url,**kwargs:io.BytesIO(contents[url.rsplit('/',1)[-1]])):
                result=await package.install(app,agent=child.agent_id,voice='irina',service=False)
            self.assertTrue(result['installed']);self.assertEqual(result['quantization'],'int8')
            self.assertFalse(app.settings.get('voice','enabled'));self.assertTrue(child.settings.get('voice','enabled'))
            self.assertEqual(app.models.roles().get('ears',[]),[])
            self.assertEqual(child.models.roles()['ears'][0]['provider'],result['provider'])
            folder=app.dirs.local/'voice';manifest=json.loads((folder/'installed.json').read_text())
            for kind,data in manifest['models'].items():
                self.assertEqual(data['revision'],'a'*40)
                for item in data['files']:self.assertEqual(hashlib.sha256((folder/'models'/kind/item['local']).read_bytes()).hexdigest(),item['hash'])
            if os.name=='posix':self.assertEqual((folder/'token').stat().st_mode&0o777,0o600)
            self.assertIn('soundfile==0.13.1',str(command.call_args_list))
            self.assertTrue((folder/'bender-voice.service').is_file())
            app.settings.set('guard',{'enabled':False})
            denied=await app.guard.check(Action('voice.install',str(folder),actor='agent'))
            self.assertFalse(denied.allowed)
        finally:await app.close()


    @async_test
    async def test_chosen_voice_is_downloaded_in_background(self):
        # Старый пакет с одной Ириной: выбор Терры молча давал Ирину.
        app=App(self.tmp/'data')
        try:
            from bender.voice import fresh
            folder=app.dirs.local/'voice';(folder/'models'/'tts').mkdir(parents=True)
            (folder/'models'/'tts'/'ru_RU-irina-medium.onnx').write_bytes(b'irina')
            (folder/'installed.json').write_text(json.dumps({'voice':'irina','tts_file':'ru_RU-irina-medium.onnx','espeak_dir':None,'quantization':'int8'}))
            stock=self.tmp/'stock-espeak';stock.mkdir()
            for name in ('phontab','ru_dict'):(stock/name).write_bytes(b'stock')
            dictionary={name:b'custom '+name.encode() for name in package.DICTIONARY['files']}
            contents={'ru_RU-terra5871-medium.onnx':b'terra','ru_RU-terra5871-medium.onnx.json':b'{}',**dictionary}
            info=lambda repo:{'sha':'b'*40,'siblings':[{'rfilename':name,'lfs':{'sha256':hashlib.sha256(contents[name]).hexdigest()}} for name in list(contents)[:2]]}
            notes=[];app.bus.listen(lambda event:notes.append(event) if event.get('type')=='notify' else None)
            self.assertTrue(fresh.missing(app,'terra'));self.assertFalse(fresh.missing(app,'irina'))
            with patch.dict(package.DICTIONARY,{'files':{name:hashlib.sha256(value).hexdigest() for name,value in dictionary.items()}}),patch.object(package,'metadata',side_effect=info),patch.object(package,'stock_espeak',return_value=stock),patch.object(package.urllib.request,'urlopen',side_effect=lambda url,**kw:io.BytesIO(contents[url.rsplit('/',1)[-1]])):
                fresh.fetch(app,'terra');fresh.fetch(app,'terra')  # вторая просьба не качает заново
                self.assertEqual(len(app._voice_fetching),1)
                await app._voice_fetching['terra']
            manifest=json.loads((folder/'installed.json').read_text())
            self.assertEqual(sorted(manifest['voices']),['irina','terra']);self.assertEqual(manifest['voice'],'irina')  # голос по умолчанию не сменился
            self.assertEqual(manifest['voices']['terra']['espeak_dir'],'espeak-ng-data');self.assertEqual(manifest['quantization'],'int8')
            self.assertFalse(fresh.missing(app,'terra'))
            self.assertIn('скачан',notes[-1]['text'])
        finally:await app.close()

    @async_test
    async def test_stale_voice_service_is_restarted(self):
        app=App(self.tmp/'data')
        try:
            from bender.voice import fresh
            answers=[{'ok':True},{'ok':True,'code':fresh.code()}];calls=[]
            async def health(_app):return answers.pop(0) if answers else {'code':fresh.code()}
            unit=self.tmp/'bender-voice.service';unit.write_text('x')
            with patch.object(fresh,'health',side_effect=health),patch('bender.core.service.unit_path',return_value=unit),patch('bender.core.service.command',side_effect=calls.append),patch.object(fresh.sys,'platform','linux'):
                await fresh.ensure(app);await fresh.ensure(app)  # второй раз не проверяет
            self.assertEqual(calls,[['systemctl','--user','restart','bender-voice.service']])
        finally:await app.close()


    def test_removed_voice_falls_back_to_default(self):
        self.assertEqual(package.chosen('denis'),package.DEFAULT);self.assertEqual(package.chosen('ruslan'),'ruslan')
        self.assertNotIn('kat',package.VOICES);self.assertIn('terra',package.VOICES)

    @async_test
    async def test_community_voice_gets_verified_dictionary_over_stock(self):
        app=App(self.tmp/'data')
        try:
            stock=self.tmp/'stock-espeak';stock.mkdir()
            for name in ('phontab','ru_dict','intonations'):(stock/name).write_bytes(b'stock '+name.encode())
            dictionary={name:b'custom '+name.encode() for name in package.DICTIONARY['files']}
            contents={'config.json':b'{}','v3_e2e_ctc_vocab.txt':b'abc','v3_e2e_ctc.int8.onnx':b'ctc-model',
                      'ru_RU-terra5871-medium.onnx':b'terra','ru_RU-terra5871-medium.onnx.json':b'{}',**dictionary}
            def info(repo):
                self.assertIn(repo,(package.REPOS['asr'],package.REPOS['tts_community']))
                names=list(contents)[:3] if repo==package.REPOS['asr'] else list(contents)[3:5]
                return {'sha':'b'*40,'siblings':[{'rfilename':name,'lfs':{'sha256':hashlib.sha256(contents[name]).hexdigest()}} for name in names]}
            hashes={name:hashlib.sha256(value).hexdigest() for name,value in dictionary.items()}
            with patch.dict(package.DICTIONARY,{'files':hashes}),patch.object(package,'metadata',side_effect=info),patch.object(package,'stock_espeak',return_value=stock),patch.object(package.platform,'machine',return_value='aarch64'),patch.object(package,'command'),patch.object(package.venv.EnvBuilder,'create',side_effect=lambda path:Path(path).mkdir(parents=True,exist_ok=True)),patch.object(package.urllib.request,'urlopen',side_effect=lambda url,**kw:io.BytesIO(contents[url.rsplit('/',1)[-1]])) as fetched:
                result=await package.install(app,voice='terra',service=False)
            self.assertEqual(result['voice'],'terra')
            urls=[call.args[0] for call in fetched.call_args_list]
            self.assertTrue(any(url.startswith('https://huggingface.co/rraaww/ru_piper/resolve/'+'b'*40) for url in urls))
            self.assertTrue(any(package.DICTIONARY['revision'] in url and 'raw.githubusercontent.com' in url for url in urls))
            folder=app.dirs.local/'voice';manifest=json.loads((folder/'installed.json').read_text())
            self.assertEqual(manifest['tts_file'],'ru_RU-terra5871-medium.onnx');self.assertEqual(manifest['espeak_dir'],'espeak-ng-data')
            built=folder/'models'/'tts'/'espeak-ng-data'
            self.assertEqual((built/'ru_dict').read_bytes(),b'custom ru_dict');self.assertEqual((built/'intonations').read_bytes(),b'stock intonations')
            self.assertEqual((stock/'ru_dict').read_bytes(),b'stock ru_dict')
            self.assertEqual(app.settings.get('voice_local','voice'),'terra')
            # Второй голос одному агенту: общий голос и прежняя модель остаются.
            child=app.agents.get(app.agents.create('Сосед',awaken=False)['id'])
            contents.update({'ru_RU-irina-medium.onnx':b'irina','ru_RU-irina-medium.onnx.json':b'{}'})
            def stock_info(repo):
                if repo==package.REPOS['asr']:return info(repo)
                names=['ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx','ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx.json']
                return {'sha':'c'*40,'siblings':[{'rfilename':name,'lfs':{'sha256':hashlib.sha256(contents[Path(name).name]).hexdigest()}} for name in names]}
            with patch.object(package,'metadata',side_effect=stock_info),patch.object(package,'stock_espeak',return_value=stock),patch.object(package.platform,'machine',return_value='aarch64'),patch.object(package,'command'),patch.object(package.venv.EnvBuilder,'create',side_effect=lambda path:Path(path).mkdir(parents=True,exist_ok=True)),patch.object(package.urllib.request,'urlopen',side_effect=lambda url,**kw:io.BytesIO(contents[url.rsplit('/',1)[-1]])):
                result=await package.install(app,agent=child.agent_id,voice='irina',service=False)
            manifest=json.loads((folder/'installed.json').read_text())
            self.assertEqual(result['voices'],['irina','terra']);self.assertEqual(manifest['voice'],'terra');self.assertEqual(manifest['tts_file'],'ru_RU-terra5871-medium.onnx')
            self.assertEqual(manifest['voices']['terra']['espeak_dir'],'espeak-ng-data');self.assertIsNone(manifest['voices']['irina']['espeak_dir'])
            self.assertEqual(child.settings.get('voice','piper'),'irina');self.assertEqual(app.settings.get('voice','piper'),'')
            self.assertEqual(app.settings.get('voice_local','voice'),'terra')
        finally:await app.close()

    def test_engine_keeps_each_agent_voice_and_sees_new_install(self):
        import sys,types
        from bender.voice.local_server import Engine
        folder=self.tmp/'voice';folder.mkdir()
        old={'voice':'irina','tts_file':'ru_RU-irina-medium.onnx','espeak_dir':None}  # манифест до нескольких голосов
        (folder/'installed.json').write_text(json.dumps(old))
        loads=[]
        class Voice:
            def __init__(self,path,kw):self.path=path;self.kw=kw
            @classmethod
            def load(cls,path,**kw):loads.append(Path(path).name);return cls(path,kw)
            configs=[]
            def synthesize_wav(self,text,target,syn_config=None):self.configs.append(syn_config);target.setnchannels(1);target.setsampwidth(2);target.setframerate(22050);target.writeframes(b'\0\0')
        with patch.dict(sys.modules,{'piper':types.SimpleNamespace(PiperVoice=Voice,SynthesisConfig=types.SimpleNamespace)}):
            engine=Engine(folder)
            engine.speak('Привет','wav');engine.speak('Привет','wav','terra')  # неизвестный голос — общий
            self.assertEqual(loads,['ru_RU-irina-medium.onnx'])
            voices={'irina':{'tts_file':'ru_RU-irina-medium.onnx','espeak_dir':None},'terra':{'tts_file':'t.onnx','espeak_dir':'espeak-ng-data'},
                    'igm':{'tts_file':'i.onnx','espeak_dir':'espeak-ng-data'},'sova':{'tts_file':'s.onnx','espeak_dir':'espeak-ng-data'}}
            (folder/'installed.json').write_text(json.dumps({**old,'voices':voices}))
            engine.speak('Привет','wav','terra')
            self.assertEqual(loads[-1],'t.onnx');self.assertEqual(engine.voices['terra'].kw,{'espeak_data_dir':folder/'models'/'tts'/'espeak-ng-data'})
            engine.speak('Привет','wav','igm');engine.speak('Привет','wav','irina');engine.speak('Привет','wav','sova')
            self.assertEqual(list(engine.voices),['igm','irina','sova'])  # не больше трёх, вытесняется давний
            with self.assertRaises(ValueError):engine.speak('Привет','wav',['terra'])
            with self.assertRaises(ValueError):engine.speak('Привет','wav','terra',5)
            engine.speak('Привет','wav','terra',1.25);self.assertAlmostEqual(engine.voices['terra'].configs[-1].length_scale,0.8)

    @async_test
    async def test_real_local_http_transcribe_speech_off_and_fallback(self):
        server=self.start_server()
        async with MindEnv(self.tmp) as env:
            app=env.app;provider=app.models.add_provider(preset='local_voice',base_url='http://127.0.0.1:'+str(server.server_address[1])+'/v1',api_key='own-test-key')
            fallback=next(key for key in app.models.providers() if key!=provider['id'])
            app.models.set_role('ears',[{'provider':provider['id'],'model':'gigaam-v3-e2e-ctc'},{'provider':fallback,'model':'ears'}])
            app.models.set_role('voice',[{'provider':provider['id'],'model':'piper-ru'}])
            app.settings.set('voice',{'enabled':True,'format':'wav'});app.settings.set('voice_local',{'enabled':True})
            transcription=await app.voice.transcribe(wav(),ext='wav',seconds=1)
            self.assertEqual(transcription['text'],'Проверка русского голоса.');self.assertEqual(server.engine.inputs,[wav()])
            speech=await app.voice.speak('Проверка');self.assertEqual(speech['mime'],'audio/wav')
            opus=await app.voice.speak('В Telegram',format='opus');self.assertTrue(opus['audio'].startswith(b'OggS'))
            app.settings.set('voice_local',{'voice':'irina'},by='owner');await app.voice.speak('Общий голос сменён')
            app.settings.set('voice',{'piper':'ruslan','speaker':'alloy'});await app.voice.speak('Свой голос')
            # без своего голоса — выбранный в настройках, смена сразу, без переустановки; alloy не уходит
            self.assertEqual(server.engine.voices,['terra','terra','irina','ruslan'])
            app.settings.set('voice',{'speed':1.3});await app.voice.speak('Быстрее')
            self.assertEqual(server.engine.speeds,[1,1,1,1,1.3])
            from bender.voice.cli import bench
            app.settings.set('voice_local',{'port':server.server_address[1]})
            (app.dirs.local/'voice').mkdir();(app.dirs.local/'voice'/'token').write_text('own-test-key')
            measured=await bench(app)
            self.assertEqual(measured['audio_seconds'],1);self.assertGreaterEqual(measured['stt']['rtf'],0)
            server.shutdown();server.server_close()
            failed=await app.voice.transcribe(wav(),ext='wav',seconds=1)
            self.assertEqual(failed['model'],'ears');self.assertTrue(failed['text'])
            app.settings.set('voice_local',{'enabled':False})
            disabled=await app.voice.transcribe(wav(),ext='wav',seconds=1)
            self.assertEqual(disabled['model'],'ears');self.assertTrue(disabled['text'])
