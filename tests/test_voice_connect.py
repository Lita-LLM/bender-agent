"""Голос любого агента: без скачивания, живой CLI и полный путь Telegram → speak."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from bender.core.errors import HumanError
from bender.core.settings import NeedsOwner
from bender.mind.passport import snapshot
from bender.tools.registry import ToolContext
from bender.voice import install as package
from bender.voice.connect import connect,connection
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv,call
from tests import test_local_voice as local
from tests import test_telegram as telegram


class VoiceConnectTest(TempDirCase):
    start_server=local.LocalVoiceTest.start_server
    until=telegram.TelegramTest.until
    sent=telegram.TelegramTest.sent

    def installed(self,app,port=None):
        if port:app.settings.set('voice_local',{'port':port})
        folder=app.dirs.local/'voice';folder.mkdir(parents=True,exist_ok=True)
        (folder/'installed.json').write_text('{}');(folder/'token').write_text('own-test-key')
        return app.models.add_provider(preset='local_voice',base_url='http://127.0.0.1:'+str(app.settings.get('voice_local','port'))+'/v1',api_key='own-test-key')

    def test_repeated_download_uses_sha256_and_git_blob_without_network(self):
        target=self.tmp/'model.onnx';raw=b'verified model';target.write_bytes(raw)
        for algorithm,digest in [('sha256',hashlib.sha256(raw).hexdigest()),('git-sha1',hashlib.sha1(('blob '+str(len(raw))+'\0').encode()+raw).hexdigest())]:
            item={'url':'https://fixture.invalid/model','algorithm':algorithm,'hash':digest}
            with patch.object(package.urllib.request,'urlopen',side_effect=AssertionError('Повторная загрузка')):
                self.assertFalse(package.download(item,target))
            target.write_bytes(b'damaged')
            with patch.object(package.urllib.request,'urlopen',return_value=io.BytesIO(raw)) as download:
                self.assertTrue(package.download(item,target));self.assertEqual(download.call_count,1)
            self.assertEqual(target.read_bytes(),raw)

    @async_test
    async def test_connect_all_preserves_own_chains_and_repairs_old_references(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;p=self.installed(app)
            empty=app.agents.get(app.agents.create('Без ушей',awaken=False)['id'])
            own=app.agents.get(app.agents.create('Свои уши',awaken=False)['id'])
            pid=app.models.roles()['main'][0]['provider']
            for role in ('ears','voice'):own.models.set_role(role,[{'provider':pid,'model':'свой-'+role}])
            bad={'provider':'потерянный','model':'старый'}
            empty.settings.replace('roles',{'ears':[bad],'voice':[bad]})
            result=await connect(ToolContext(app,actor='owner'),'all',only_empty=True)
            self.assertEqual(set(result['connected']),{'main',empty.agent_id});self.assertEqual(result['skipped'],[own.agent_id])
            self.assertEqual(result['repaired'],2);self.assertTrue(connection(empty)['connected'])
            self.assertEqual(own.models.roles()['ears'][0]['model'],'свой-ears')
            self.assertTrue(empty.settings.get('voice','enabled'))
            self.assertEqual(empty.models.roles()['voice'][0]['provider'],p['id'])
            app.settings.set('voice',{'enabled':False});app.models.set_role('ears',[])
            self.assertTrue(empty.settings.get('voice','enabled'));self.assertTrue(connection(empty)['roles']['ears'])

    @async_test
    async def test_helper_connects_itself_but_cannot_change_raw_audio_or_neighbour(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;self.installed(app)
            child=app.agents.get(app.agents.create('Помощник',awaken=False)['id'])
            before=snapshot(child)['voice'];self.assertTrue(before['local']['installed'])
            self.assertIn('voice_connect',before['connect'])
            result=await child.tools.run('voice_connect',{},ToolContext(child,session_id=child.main_session()))
            self.assertTrue(result.ok,result.content);self.assertTrue(connection(child)['connected'])
            self.assertFalse(connection(app)['connected'])
            self.assertEqual(child.settings.get('voice','stt_mode'),'transcribe')
            with self.assertRaises(NeedsOwner):child.settings.set('voice',{'stt_mode':'native'},by='agent')
            with self.assertRaises(HumanError):await connect(ToolContext(child),'main')
            data=snapshot(child)['voice'];self.assertTrue(data['enabled']);self.assertEqual(data['roles']['ears'][0]['location'],'локальная')
            self.assertFalse(env.approvals)

    @async_test
    async def test_default_install_connects_permanent_agents_and_reuses_files(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;child=app.agents.get(app.agents.create('Второй',awaken=False)['id'])
            contents={'config.json':b'{}','v3_e2e_ctc_vocab.txt':b'abc','v3_e2e_ctc.int8.onnx':b'ctc-model',
                      'ru_RU-irina-medium.onnx':b'piper-model','ru_RU-irina-medium.onnx.json':b'{}'}
            def info(repo):
                names=list(contents)[:3] if repo==package.REPOS['asr'] else ['ru/ru_RU/irina/medium/'+n for n in list(contents)[3:]]
                return {'sha':'a'*40,'siblings':[{'rfilename':n,'lfs':{'sha256':hashlib.sha256(contents[Path(n).name]).hexdigest()}} for n in names]}
            with patch.object(package,'metadata',side_effect=info),patch.object(package.platform,'machine',return_value='aarch64'),patch.object(package,'command'),patch.object(package.venv.EnvBuilder,'create',side_effect=lambda path:Path(path).mkdir(exist_ok=True)),patch.object(package.urllib.request,'urlopen',side_effect=lambda url,**kw:io.BytesIO(contents[url.rsplit('/',1)[-1]])) as download:
                first=await package.install(app,voice='irina',service=False)
                self.assertEqual(set(first['connected']),{'main',child.agent_id});self.assertEqual(download.call_count,5)
                second=await package.install(app,voice='irina',service=False)
                self.assertEqual(download.call_count,5);self.assertEqual(second['reused_files'],5);self.assertEqual(second['downloaded_files'],0)
                self.assertIn('/voice reply',second['note']);self.assertIn('Перезапуск ядра не нужен',second['note'])

    @async_test
    async def test_external_cli_then_telegram_voice_transcript_speak_in_same_child_topic(self):
        server=self.start_server()
        async with MindEnv(self.tmp) as env:
            app=env.app;self.installed(app,server.server_address[1]);app.settings.set('voice_local',{'enabled':True})
            child=app.agents.get(app.agents.create('Слушатель',awaken=False)['id'])
            self.assertFalse(child.settings.get('voice','enabled'));self.assertEqual(child.models.roles().get('ears',[]),[])
            env.fake.state.tg_topics=True
            await telegram.TelegramTest._up(self,env,owner=True);tg=app.telegram
            sid=child.store.new_session('chat','Голосовой разговор');address=await tg.topics.create(child,42,sid)
            result=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','bender','--data',str(app.dirs.root),'voice','connect',child.agent_id],capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            env.script([call('speak',text='Я слышу и отвечаю голосом.',complete=True),{'text':'Ответ уже отправлен.'}])
            await tg._handle({'message':{'message_id':915,'from':{'id':42},'chat':{'id':42,'type':'private'},'message_thread_id':address.thread,
                                         'voice':{'file_id':'fixture','duration':12}}})
            await self.until(lambda:bool(self.sent(env,'sendVoice')))
            await self.until(lambda:not tg._bg)
            voice=self.sent(env,'sendVoice');self.assertEqual(len(voice),1)
            self.assertEqual(int(voice[0]['chat_id']),42);self.assertEqual(int(voice[0]['message_thread_id']),address.thread)
            user=next(row for row in child.store.messages(sid) if row['role']=='user')
            text=json.dumps(user['content'],ensure_ascii=False)
            self.assertIn('[голосовое хозяина, 0:12, расшифровка GigaAM]',text)
            self.assertIn('Проверка русского голоса.',text);self.assertFalse(user.get('meta',{}).get('untrusted',False))
            self.assertTrue(child.settings.get('voice','enabled'));self.assertFalse(app.settings.get('voice','enabled'))
            self.assertEqual(child.store.one("SELECT COUNT(*) FROM calls WHERE role='ears'")[0],1)
            self.assertEqual(child.store.one("SELECT COUNT(*) FROM calls WHERE role='voice'")[0],1)
            self.assertFalse(self.sent(env,'sendMessage') and any(m.get('text')=='Ответ уже отправлен.' for m in self.sent(env)))

    @async_test
    async def test_reply_command_and_speak_fallback_are_explicit_without_new_model_turn(self):
        async with MindEnv(self.tmp) as env:
            await telegram.TelegramTest._up(self,env,owner=True);app=env.app;tg=app.telegram
            await tg._handle({'message':{'message_id':920,'from':{'id':42},'chat':{'id':42,'type':'private'},'text':'/voice reply'}})
            self.assertTrue(app.settings.get('voice','enabled'));self.assertTrue(app.settings.get('voice','telegram_reply'))
            self.assertFalse(app.mind.turns)
            app.store.update_session(env.sid,meta={'home_channel':'telegram'})
            with patch.object(app.voice,'speak',side_effect=HumanError('voice','Нет OGG/Opus','Настрой Голос.')):
                result=await app.tools.run('speak',{'text':'Текст хозяину','complete':True},ToolContext(app,session_id=env.sid))
            self.assertTrue(result.ok,result.content);self.assertTrue(result.data['fallback'])
            self.assertIn('Нет OGG/Opus',self.sent(env)[-1]['text']);self.assertFalse(self.sent(env,'sendVoice'))

    @async_test
    async def test_speak_in_pult_has_playable_attachment_and_refuses_other_sessions_or_groups(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            app.models.set_role('voice',[{'provider':pid,'model':'voice'}]);app.settings.set('voice',{'enabled':True})
            result=await app.tools.run('speak',{'text':'Голос в пульте'},ToolContext(app,session_id=env.sid))
            self.assertTrue(result.ok,result.content)
            attachment=result.data['attachments'][0];self.assertTrue(Path(attachment['path']).read_bytes().startswith(b'RIFF'))
            self.assertTrue(any(row.get('kind')=='attachment' for row in app.store.messages(env.sid)))
            sid=app.store.new_session('correspondence','Письмо')
            denied=await app.tools.run('speak',{'text':'Не отправлять'},ToolContext(app,session_id=sid))
            self.assertFalse(denied.ok)
            await telegram.TelegramTest._up(self,env,owner=True)
            app.store.update_session(env.sid,meta={'home_channel':'telegram'})
            denied=await app.tools.run('speak',{'text':'Не отправлять группе'},ToolContext(app,session_id=env.sid,turn={'_telegram_route':{'chat':-99}}))
            self.assertFalse(denied.ok);self.assertFalse(self.sent(env,'sendVoice'))

    @async_test
    async def test_forwarded_transcript_keeps_confidence_gaps_taint_and_is_not_slash_command(self):
        server=self.start_server()
        async with MindEnv(self.tmp) as env:
            app=env.app;self.installed(app,server.server_address[1]);app.settings.set('voice_local',{'enabled':True})
            await connect(ToolContext(app,actor='owner'))
            await telegram.TelegramTest._up(self,env,owner=True)
            with patch.object(server.engine,'transcribe',return_value={'text':'/shutdown','confidence':.8,'gaps':['0:03–0:04']}):
                env.script([{'text':'Это пересланная речь.'}])
                await app.telegram._handle({'message':{'message_id':930,'from':{'id':42},'chat':{'id':42,'type':'private'},
                    'forward_origin':{'type':'user'},'voice':{'file_id':'fixture','duration':4}}})
                await self.until(lambda:any(m.get('text')=='Это пересланная речь.' for m in self.sent(env)))
                await self.until(lambda:not app.telegram._bg)
            user=next(row for row in app.store.messages(env.sid) if row['role']=='user');text=json.dumps(user['content'],ensure_ascii=False)
            self.assertIn('пересланное голосовое, недоверенные данные',text);self.assertIn('уверенность 80%',text);self.assertIn('пропуски: 0:03–0:04',text)
            self.assertIn('/shutdown',text);self.assertTrue(user['meta']['untrusted']);self.assertTrue(app.store.session(env.sid)['meta']['last_turn']['tainted'])
            self.assertFalse(app.guard.pending)
