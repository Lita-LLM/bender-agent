"""Файл становится медиа только после fs.read, пакеты тулов остаются целыми."""
import base64
import json
from pathlib import Path

from bender.mind import compact
from bender.net.http_server import JSONResponse
from bender.router.probes import png
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv,call
from tests.test_audio_files import wav
from tests import test_chatgpt as subscription


class ReadFileMediaTest(TempDirCase):
    @async_test
    async def test_openai_and_anthropic_get_image_after_all_tool_results(self):
        for kind in ('openai','anthropic'):
            with self.subTest(kind=kind):
                async with MindEnv(self.tmp/kind) as env:
                    app=env.app;pid=app.models.roles()['main'][0]['provider']
                    app.models.update_provider(pid,{'kind':kind})
                    image=app.dirs.work/'цвет.png';raw=png((255,0,0));image.write_bytes(raw)
                    env.script([{'tool_calls':[{'name':'read_file','arguments':{'path':image.name}}, {'name':'list_dir','arguments':{}}]}, {'text':'Красная картинка.'}])
                    result=await app.mind.say(env.sid,'Открой файл и посмотри папку')
                    self.assertIsNone(result['error'],result);self.assertTrue(result['tainted'])
                    rows=app.store.messages(env.sid)
                    self.assertEqual([m['role'] for m in rows],['user','assistant','tool','tool','user','assistant'])
                    self.assertTrue(rows[4]['meta']['tool_media']);self.assertTrue(rows[4]['meta']['untrusted'])
                    self.assertNotIn(base64.b64encode(raw).decode(),json.dumps(rows[2]['meta']))
                    request=[r for r in env.fake.state.requests if r.get('api')==kind][-1]['body']['messages']
                    if kind=='openai':
                        self.assertEqual([m['role'] for m in request[-3:]],['tool','tool','user'])
                        part=next(p for p in request[-1]['content'] if p['type']=='image_url')
                        self.assertEqual(base64.b64decode(part['image_url']['url'].split(',')[1]),raw)
                    else:
                        blocks=request[-1]['content'];types=[p['type'] for p in blocks]
                        self.assertEqual(types[:2],['tool_result','tool_result'])
                        self.assertGreater(types.index('image'),1)
                        self.assertEqual(base64.b64decode(next(p for p in blocks if p['type']=='image')['source']['data']),raw)
                    self.assertIn('недоверенные данные',json.dumps(request,ensure_ascii=False))

    @async_test
    async def test_chatgpt_responses_gets_attachment_after_function_outputs(self):
        async with subscription.Env(self.tmp) as env:
            await env.login();env.use('script');app=env.app;sid=app.main_session()
            path=app.dirs.work/'photo.png';raw=png((0,255,0));path.write_bytes(raw)
            env.fake.state.script=[{'tool_calls':[{'name':'read_file','arguments':{'path':path.name}}, {'name':'list_dir','arguments':{}}]}, {'text':'Зелёный.'}]
            result=await app.mind.say(sid,'Открой')
            self.assertIsNone(result['error'],result)
            items=[r for r in env.fake.state.requests if r.get('api')=='chatgpt'][-1]['body']['input']
            attachment=next(i for i,item in enumerate(items) if any(p.get('type')=='input_image' for p in item.get('content',[]) if isinstance(p,dict)))
            outputs=[i for i,item in enumerate(items) if item.get('type')=='function_call_output']
            self.assertEqual(len(outputs),2);self.assertGreater(attachment,max(outputs))
            self.assertEqual(items[attachment]['role'],'user')
            self.assertIn('недоверенные данные',json.dumps(items[attachment],ensure_ascii=False))

    @async_test
    async def test_old_images_are_paths_and_read_file_can_reopen_without_changing_archive(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;path=app.dirs.work/'old.png';raw=png((0,0,255));path.write_bytes(raw)
            app.settings.set('attachments',{'history_turns':2})
            env.script([call('read_file',path=path.name),{'text':'Вижу.'}])
            await app.mind.say(env.sid,'Открой')
            await app.mind.say(env.sid,'Второй ход')
            self.assertIn('image_url',json.dumps(env.fake.state.requests[-1]['body']))
            await app.mind.say(env.sid,'Третий ход')
            sent=json.dumps(env.fake.state.requests[-1]['body']['messages'],ensure_ascii=False)
            self.assertNotIn('image_url',sent);self.assertIn(str(path),sent);self.assertIn('read_file',sent)
            original=next(m for m in app.store.messages(env.sid) if m['meta'].get('tool_media'))
            self.assertEqual(original['content'][1]['data'],base64.b64encode(raw).decode())
            env.script([call('read_file',path=path.name),{'text':'Снова вижу.'}])
            result=await app.mind.say(env.sid,'Посмотри снова')
            self.assertIsNone(result['error'],result)
            self.assertEqual(json.dumps(env.fake.state.requests[-1]['body']).count('image_url'),2)  # ключ и объект
            self.assertIn('Посмотри снова',compact.continuation(app,env.sid,app.store.messages(env.sid),{}))

    @async_test
    async def test_limits_accumulate_across_tool_steps_and_reset_on_next_turn(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;path=app.dirs.work/'small.png';path.write_bytes(png((1,2,3)))
            app.settings.set('attachments',{'read_files_per_turn':1})
            env.script([call('read_file',path=path.name),call('read_file',path=path.name),{'text':'Лимит.'}])
            result=await app.mind.say(env.sid,'Дважды')
            self.assertIsNone(result['error'],result)
            tools=[m for m in app.store.messages(env.sid) if m['role']=='tool']
            self.assertTrue(tools[0]['meta']['ok']);self.assertFalse(tools[1]['meta']['ok']);self.assertIn('Лимит медиафайлов',tools[1]['content'])
            env.script([call('read_file',path=path.name),{'text':'Новый ход.'}]);await app.mind.say(env.sid,'Снова')
            self.assertTrue([m for m in app.store.messages(env.sid) if m['role']=='tool'][-1]['meta']['ok'])
            app.settings.set('attachments',{'read_mb_per_turn':1,'read_files_per_turn':4})
            path.write_bytes(png((1,2,3))+b'\0'*(600*1024))
            ctx=ToolContext(app,session_id=env.sid)
            first=await app.tools.run('read_file',{'path':path.name},ctx)
            second=await app.tools.run('read_file',{'path':path.name},ctx)
            self.assertTrue(first.ok,first.content);self.assertFalse(second.ok);self.assertIn('больше лимита',second.content)
            large=app.dirs.work/'large.png'
            with large.open('wb') as out:out.write(png((1,2,3)));out.truncate(21*1024*1024)
            too_big=await app.tools.run('read_file',{'path':large.name},ToolContext(app,session_id=env.sid))
            self.assertFalse(too_big.ok);self.assertFalse(too_big.model_parts)

    @async_test
    async def test_fs_read_denial_and_switches_never_send_bytes(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;path=app.dirs.work/'secret.png';path.write_bytes(png((1,2,3)))
            rule=app.guard.add_rule('fs.read',str(path),'deny')
            ctx=ToolContext(app,session_id=env.sid)
            denied=await app.tools.run('read_file',{'path':path.name},ctx)
            self.assertFalse(denied.ok);self.assertFalse(denied.model_parts);self.assertFalse(ctx.turn)
            app.guard.remove_rule(rule['id']);app.settings.set('attachments',{'enabled':False})
            disabled=await app.tools.run('read_file',{'path':path.name},ctx)
            self.assertFalse(disabled.ok);self.assertFalse(disabled.model_parts)
            app.settings.set('attachments',{'enabled':True,'vision':False})
            disabled=await app.tools.run('read_file',{'path':path.name},ctx)
            self.assertFalse(disabled.ok)
            other=app.agents.create('Другой',agent_id='peer',awaken=False)
            peer=app.agents.get('peer');foreign=peer.dirs.work/'hidden.png';foreign.write_bytes(png((1,2,3)))
            denied=await app.tools.run('read_file',{'path':str(foreign)},ctx)
            self.assertFalse(denied.ok);self.assertIn('другого агента',denied.content)

    @async_test
    async def test_audio_helper_transcribes_real_formats_and_obeys_audio_denial(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            app.agents.create('Помощник',agent_id='helper',awaken=False);ctx=app.agents.get('helper');sid=ctx.main_session()
            ctx.models.set_role('ears',[{'provider':pid,'model':'ears'}]);ctx.settings.set('voice',{'enabled':True})
            folder=Path(__file__).with_name('fixtures')/'audio'
            for ext,raw in [('wav',wav())]+[(ext,(folder/('two-seconds.'+ext)).read_bytes()) for ext in ('mp3','ogg','flac','m4a')]:
                path=ctx.dirs.work/('test.'+ext);path.write_bytes(raw)
                result=await ctx.tools.run('read_file',{'path':path.name},ToolContext(ctx,session_id=sid))
                self.assertTrue(result.ok,result.content);self.assertTrue(result.tainted)
                self.assertIn('Привет голосом',result.content);self.assertFalse(result.model_parts)
            self.assertEqual(ctx.store.one("SELECT count(*) FROM calls WHERE role='ears'")[0],5)
            ctx.guard.add_rule('audio.transcribe','ears','deny',only_clean=False)
            denied=await ctx.tools.run('read_file',{'path':path.name},ToolContext(ctx,session_id=sid))
            self.assertFalse(denied.ok);self.assertIn('Правило хозяина',denied.content)
            self.assertEqual(ctx.store.one("SELECT count(*) FROM calls WHERE role='ears'")[0],5)
            self.assertEqual(len([r for r in env.fake.state.requests if r.get('api')=='audio_stt']),5)
            ctx.guard.remove_rule(ctx.guard.rules()[-1]['id'])
            capabilities=app.agents.rights.effective(ctx)['capabilities']
            ctx.settings.set('agent_rights',{'capabilities':[cap for cap in capabilities if cap!='audio.transcribe']})
            denied=await ctx.tools.run('read_file',{'path':path.name},ToolContext(ctx,session_id=sid))
            self.assertFalse(denied.ok);self.assertIn('Способность',denied.content)
            self.assertEqual(len([r for r in env.fake.state.requests if r.get('api')=='audio_stt']),5)

    @async_test
    async def test_image_file_is_described_by_vision_and_text_binary_stays_compatible(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            app.models.set_capability(pid,'script','vision',False)
            @env.fake.route('POST','/look/v1/chat/completions')
            async def look(req):
                body=req.json();env.fake.state.requests.append({'api':'openai','body':body})
                return JSONResponse({'choices':[{'message':{'role':'assistant','content':'Красный круг.'},'finish_reason':'stop'}]})
            look_provider=app.models.add_provider(name='Зрение',base_url=env.base+'/look/v1',extra={'is_local':True})
            app.models.set_role('vision',[{'provider':look_provider['id'],'model':'look'}])
            path=app.dirs.work/'picture.png';path.write_bytes(png((255,0,0)))
            env.script([call('read_file',path=path.name),{'text':'На картинке красный круг.'}])
            result=await app.mind.say(env.sid,'Что в файле?')
            self.assertIsNone(result['error'],result);self.assertTrue(result['tainted'])
            sent=[r['body'] for r in env.fake.state.requests if r.get('api')=='openai']
            self.assertEqual([r['model'] for r in sent],['script','look','script'])
            self.assertNotIn('image_url',json.dumps(sent[-1]));self.assertIn('Красный круг',json.dumps(sent[-1]['messages'],ensure_ascii=False))
            text=app.dirs.work/'text.txt';text.write_text('Первая\nВторая\nТретья\n')
            output=await app.tools.run('read_file',{'path':text.name,'offset':2,'limit':1},ToolContext(app,session_id=env.sid))
            self.assertTrue(output.ok);self.assertIn('Вторая',output.content);self.assertNotIn('Первая',output.content);self.assertFalse(output.model_parts)
            binary=app.dirs.work/'binary.bin';binary.write_bytes(b'\0\1\2')
            output=await app.tools.run('read_file',{'path':binary.name},ToolContext(app,session_id=env.sid))
            self.assertFalse(output.ok);self.assertIn('двоичный',output.content);self.assertFalse(output.model_parts)

    @async_test
    async def test_native_audio_from_file_is_user_attachment_and_model_no_uses_ears(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            app.models.set_role('ears',[{'provider':pid,'model':'ears'}]);app.settings.set('voice',{'enabled':True,'stt_mode':'native'})
            path=app.dirs.work/'record.wav';path.write_bytes(wav())
            env.script([call('read_file',path=path.name),{'text':'Услышал.'}])
            result=await app.mind.say(env.sid,'Слушай файл')
            self.assertIsNone(result['error'],result);self.assertTrue(result['tainted'])
            user=env.fake.state.requests[-1]['body']['messages'][-1]
            self.assertEqual(user['role'],'user');self.assertTrue(any(p['type']=='input_audio' for p in user['content']))
            self.assertEqual(app.store.one("SELECT count(*) FROM calls WHERE role='ears'")[0],0)
            app.models.set_capability(pid,'script','audio_input',False)
            env.script([call('read_file',path=path.name),{'text':'Расшифровал.'}])
            result=await app.mind.say(env.sid,'Ещё раз')
            self.assertIsNone(result['error'],result)
            request=json.dumps([r for r in env.fake.state.requests if r.get('api')=='openai'][-1]['body']['messages'],ensure_ascii=False)
            self.assertNotIn('input_audio',request);self.assertIn('Привет голосом',request)
            self.assertGreater(app.store.one("SELECT count(*) FROM calls WHERE role='ears'")[0],0)
