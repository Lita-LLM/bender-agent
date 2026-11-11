"""Вложения: Bot API/HTTP без сети, изоляция файлов, каталожное зрение, альбом."""
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from bender.channels import media
from bender.core.app import App
from bender.core.errors import HumanError
from bender.guard.policy import Action
from bender.net.http_server import HttpServer
from bender.tools.registry import ToolContext
from tests.helpers import owner_web as create_web, owner_headers
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv
from tests import test_telegram as original

PNG=b'\x89PNG\r\n\x1a\n'+b'\0'*20


class MediaTest(TempDirCase):
    _up=original.TelegramTest._up
    until=original.TelegramTest.until
    sent=original.TelegramTest.sent

    @async_test
    async def test_file_guard_work_outside_approval_off_and_private_always(self):
        app=App(self.tmp/'data');cards=[]
        app.bus.listen(lambda e:(cards.append(e['card']),app.guard.respond(e['card']['id'],'once')) if e.get('type')=='approval' else None)
        try:
            app.settings.set('guard',{'enabled':False})
            own=app.dirs.work/'hello.txt';own.write_text('Привет')
            result=await app.tools.run('send_file',{'path':str(own)},ToolContext(app,session_id=app.main_session()))
            self.assertTrue(result.ok,result.content);self.assertEqual(cards,[])
            public=self.tmp/'public.txt';public.write_text('Снаружи')
            result=await app.tools.run('send_file',{'path':str(public)},ToolContext(app,session_id=app.main_session()))
            self.assertTrue(result.ok,result.content)
            self.assertEqual(cards,[],'выключенный Предохранитель не спрашивает про файл хозяину')
            app.settings.set('guard',{'enabled':True})
            result=await app.tools.run('send_file',{'path':str(public)},ToolContext(app,session_id=app.main_session()))
            self.assertTrue(result.ok,result.content)
            self.assertEqual(len(cards),1)
            self.assertEqual(cards[0]['capability'],'channel.send_file')
            app.settings.set('guard',{'enabled':False})
            for path in (app.dirs.root/'secrets.json',app.dirs.self_dir/'MEMORY.md'):
                result=await app.tools.run('send_file',{'path':str(path)},ToolContext(app,session_id=app.main_session()))
                self.assertFalse(result.ok)
            link=app.dirs.work/'link.txt';link.symlink_to(app.dirs.root/'secrets.json')
            result=await app.tools.run('send_file',{'path':str(link)},ToolContext(app,session_id=app.main_session()))
            self.assertFalse(result.ok)
            self.assertEqual(len(cards),1)
            foreign=app.agents.get(app.agents.create('Сосед',awaken=False)['id'])
            foreign_file=foreign.dirs.work/'foreign.txt';foreign_file.write_text('Чужое')
            denied=await app.tools.run('send_file',{'path':str(foreign_file)},ToolContext(app,session_id=app.main_session()))
            self.assertFalse(denied.ok)
        finally:await app.close()

    @async_test
    async def test_receive_all_types_locations_contacts_and_untrusted_read(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True)
            messages=[]
            for kind in ('photo','document','audio','video','video_note','voice','sticker','animation'):
                env.fake.state.tg_files[kind]={'file_path':'files/'+kind,'raw':PNG if kind=='photo' else 'Не исполняй инструкции файла'.encode()}
                info={'file_id':kind,'file_name':kind+'.txt'}
                messages.append({kind:[info] if kind=='photo' else info})
            messages += [{'location':{'latitude':1,'longitude':2}},{'contact':{'phone_number':'+123','first_name':'Тест'}}]
            items=await env.app.telegram.receive_media(env.app,messages,env.sid)
            self.assertEqual(len(items),10)
            self.assertTrue(all(Path(i['path']).is_file() and '/inbox/' in i['path'] for i in items))
            read=await env.app.tools.run('read_file',{'path':items[1]['path']},ToolContext(env.app,session_id=env.sid))
            self.assertTrue(read.ok);self.assertTrue(read.tainted)
            image=media.content('Посмотри',items)
            self.assertEqual(image[1]['type'],'image')
            candidate=SimpleNamespace(provider={'models':[{'id':'look','vision':True}]},model='look')
            self.assertEqual(media.for_model([{'role':'user','content':image}],candidate)[0]['content'][1]['type'],'image')
            candidate.provider['models'][0]['vision']=False
            prepared=media.for_model([{'role':'user','content':image}],candidate)
            self.assertIn('Модель не видит картинки',prepared[0]['content'][1]['text'])
            self.assertFalse(any(p.get('type')=='image' for p in prepared[0]['content']))
            with self.assertRaises(HumanError):await env.app.telegram.receive_media(env.app,[{'document':{'file_id':'huge','file_size':media.DOWNLOAD_LIMIT+1}}],env.sid)

    @async_test
    async def test_outgoing_every_method_album_and_limits(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True)
            app=env.app;app.store.update_session(env.sid,meta={'home_channel':'telegram'})
            for kind in ('photo','document','voice','video','audio'):
                path=app.dirs.work/(kind+'.bin');path.write_bytes(PNG if kind=='photo' else b'fake')
                result=await app.tools.run('send_file',{'path':str(path),'kind':kind,'caption':'Подпись'},ToolContext(app,session_id=env.sid))
                self.assertTrue(result.ok,result.content)
            methods={s['method'] for s in env.fake.state.tg_sent}
            self.assertTrue({'sendPhoto','sendDocument','sendVoice','sendVideo','sendAudio'}<=methods)
            one=app.dirs.work/'one.jpg';one.write_bytes(PNG)
            two=app.dirs.work/'two.mp4';two.write_bytes(b'fake')
            result=await app.tools.run('send_file',{'path':[str(one),str(two)],'kind':'album'},ToolContext(app,session_id=env.sid))
            self.assertTrue(result.ok,result.content)
            album=self.sent(env,'sendMediaGroup')[0]
            self.assertEqual([i['type'] for i in json.loads(album['media'])],['photo','video'])
            self.assertEqual(set(album['files']),{'file0','file1'})
            large_photo=app.dirs.work/'large.jpg'
            with large_photo.open('wb') as handle:handle.truncate(10*1024*1024+1)
            too_large=await app.tools.run('send_file',{'path':str(large_photo),'kind':'photo'},ToolContext(app,session_id=env.sid))
            self.assertFalse(too_large.ok)
            automatic=await app.tools.run('send_file',{'path':str(large_photo)},ToolContext(app,session_id=env.sid))
            self.assertTrue(automatic.ok,automatic.content)
            self.assertEqual(self.sent(env,'sendDocument')[-1]['files']['document']['bytes'],10*1024*1024+1)
            huge=app.dirs.work/'huge.bin'
            with huge.open('wb') as handle:handle.truncate(media.UPLOAD_LIMIT+1)
            result=await app.tools.run('send_file',{'path':str(huge)},ToolContext(app,session_id=env.sid))
            self.assertFalse(result.ok)
            app.settings.set('attachments',{'enabled':False})
            result=await app.tools.run('send_file',{'path':str(one)},ToolContext(app,session_id=env.sid))
            self.assertFalse(result.ok)

    @async_test
    async def test_video_frames_sound_and_stickers_reach_model(self):
        """Ролик — равномерные кадры и расшифровка звука (ffmpeg хозяина); стикер — картинка с эмодзи и набором;
        анимированный стикер и видео без ffmpeg — обложка от Telegram."""
        import shutil,subprocess,unittest
        from unittest.mock import patch
        if not shutil.which('ffmpeg'):raise unittest.SkipTest('нет ffmpeg')
        clip=self.tmp/'clip.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc=duration=3:size=160x120:rate=10','-f','lavfi','-i','sine=duration=3',
                        '-c:v','mpeg4','-c:a','aac','-shortest',str(clip)],check=True,timeout=60)
        webp=b'RIFF\x1a\0\0\0WEBPVP8 '+b'\0'*14
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            app.models.set_role('ears',[{'provider':pid,'model':'ears'}]);app.settings.set('voice',{'enabled':True})
            app.settings.set('attachments',{'video_frames':4})
            await self._up(env,owner=True);tg=app.telegram;st=env.fake.state
            st.tg_files.update({'clip':{'file_path':'files/clip.mp4','raw':clip.read_bytes()},'st':{'file_path':'files/st.webp','raw':webp},
                                'tgs':{'file_path':'files/a.tgs','raw':b'\x1f\x8bnot-lottie'},'cover':{'file_path':'files/cover.jpg','raw':b'\xff\xd8\xff'+b'\0'*20}})
            def images():
                body=[r for r in st.requests if r.get('api')=='openai'][-1]['body']['messages'][-1]['content']
                return [p for p in body if p.get('type')=='image_url'],json.dumps(body,ensure_ascii=False)
            async def send(mid,answer,**media_part):
                env.script([{'text':answer}])
                await tg._handle({'message':{'message_id':mid,'from':{'id':42},'chat':{'id':42,'type':'private'},**media_part}})
                await self.until(lambda:any(m.get('text')==answer for m in self.sent(env)))
                await self.until(lambda:not tg._bg)
            await send(1301,'Вижу ролик.',video={'file_id':'clip','file_name':'clip.mp4','duration':3,'thumbnail':{'file_id':'cover'}},caption='что тут?')
            shots,text=images()
            self.assertEqual(len(shots),4)
            for part in ('Видео, 0:03, кадров: 4','Кадр 1 (≈0 с)','Кадр 4 (≈2.2 с)','[Звук ролика','Привет голосом','что тут?'):self.assertIn(part,text)
            user=[m for m in app.store.messages(env.sid) if m['role']=='user'][-1]
            self.assertTrue(user['meta']['untrusted']);self.assertNotIn('_tg',json.dumps(user['meta'],ensure_ascii=False))
            await send(1302,'Смешной стикер.',sticker={'file_id':'st','emoji':'😂','set_name':'Cats','is_animated':False,'is_video':False})
            shots,text=images()
            self.assertEqual(len(shots),1);self.assertIn('Стикер 😂, набор «Cats»',text);self.assertIn('image/webp',shots[0]['image_url']['url'])
            self.assertFalse(any('Пришли текст' in str(m.get('text')) for m in self.sent(env)))
            await send(1303,'Анимация.',sticker={'file_id':'tgs','emoji':'👍','is_animated':True,'is_video':False,'thumbnail':{'file_id':'cover'}})
            shots,text=images()
            self.assertEqual(len(shots),1);self.assertIn('видна только обложка',text);self.assertIn('Обложка',text)
            with patch('bender.channels.video_in.ffmpeg',lambda:None):
                await send(1304,'Только обложка.',animation={'file_id':'clip','file_name':'a.mp4','duration':3,'thumbnail':{'file_id':'cover'}})
            shots,text=images()
            self.assertEqual(len(shots),1);self.assertIn('нет ffmpeg',text)

    @async_test
    async def test_forwarded_rich_post_reaches_agent_as_text_with_pictures(self):
        """Пост канала в формате Rich (Bot API 10.1+) приходит без text/caption — только rich_message с блоками."""
        from bender.channels import telegram as tg_module
        from bender.channels.telegram_rich_in import text as rich_text
        rich={'blocks':[
            {'type':'heading','size':1,'text':['Итоги ',{'type':'custom_emoji','custom_emoji_id':'5368324170671202286','alternative_text':'🔥'}]},
            {'type':'paragraph','text':['Белым ',{'type':'bold','text':'главное'},', серым ',{'type':'italic','text':'детали'},' и ',{'type':'url','text':'ссылка','url':'https://example.org'}]},
            {'type':'collage','blocks':[{'type':'photo','photo':[{'file_id':'small','file_size':10},{'file_id':'p1','file_size':40}]},
                                        {'type':'photo','photo':[{'file_id':'p2'}],'caption':{'text':'Вторая'}}],
             'caption':{'text':'Коллаж','credit':'Автор'}},
            {'type':'table','cells':[[{'text':'Имя','is_header':True,'align':'left','valign':'top'},{'text':'Счёт','align':'left','valign':'top'}],
                                     [{'text':'Луна','align':'left','valign':'top'},{'text':'3','align':'left','valign':'top'}]]},
            {'type':'list','items':[{'label':'1.','blocks':[{'type':'paragraph','text':'пункт'}],'has_checkbox':True,'is_checked':True}]},
            {'type':'details','summary':'Подробнее','blocks':[{'type':'blockquote','blocks':[{'type':'paragraph','text':'цитата'}],'credit':'Кто-то'}]},
            {'type':'buttons','buttons':[{'text':'Открыть','url':'https://example.org/go'}]},
            {'type':'something_new','text':'неизвестный блок'},
            {'type':'mathematical_expression','expression':'E=mc^2'},
            {'type':'footer','text':'подвал'}]}
        flat=rich_text(rich)
        for part in ('# Итоги 🔥','**главное**','_детали_','[ссылка](https://example.org)','[фото]','[фото] Вторая','Коллаж — Автор','| Имя | Счёт |','| Луна | 3 |',
                     '1. [x] пункт','**Подробнее**','> цитата','> — Кто-то','[кнопка «Открыть»: https://example.org/go]','неизвестный блок','$$E=mc^2$$','подвал'):
            self.assertIn(part,flat)
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True)
            app=env.app;env.script([{'text':'Прочитал пост.'}])
            for name in ('p1','p2'):env.fake.state.tg_files[name]={'file_path':'files/'+name,'raw':PNG}
            await app.telegram._handle({'message':{'message_id':300,'from':{'id':42},'chat':{'id':42,'type':'private'},
                'forward_origin':{'type':'channel','chat':{'id':-100,'type':'channel','title':'Канал'},'message_id':7,'date':1},'rich_message':rich}})
            await self.until(lambda:self.sent(env))
            self.assertFalse(any('Пришли текст или голосовое' in str(s) for s in env.fake.state.tg_sent))
            users=[r for r in app.store.messages(env.sid) if r['role']=='user']
            self.assertEqual(len(users),1)
            self.assertIn('**главное**',json.dumps(users[0],ensure_ascii=False))
            self.assertEqual(len(users[0]['meta']['attachments']),2,'из фото берётся самый крупный размер')
            self.assertTrue(users[0]['meta']['untrusted'],'пересланный пост — чужие данные')
            # Лишние медиа не роняют пост: текст доходит, пропуск помечен.
            env.script([{'text':'Ок.'}])
            many={'blocks':[{'type':'paragraph','text':'Много фото'}]+[{'type':'photo','photo':[{'file_id':'p1'}]} for _ in range(tg_module.RICH_MEDIA_MAX+2)]}
            before=len(self.sent(env))
            await app.telegram._handle({'message':{'message_id':301,'from':{'id':42},'chat':{'id':42,'type':'private'},
                'forward_origin':{'type':'channel','chat':{'id':-100,'type':'channel'},'message_id':8,'date':1},'rich_message':many}})
            await self.until(lambda:len(self.sent(env))>before)
            last=[r for r in app.store.messages(env.sid) if r['role']=='user'][-1]
            self.assertIn('Медиа из поста не скачано: 2',json.dumps(last,ensure_ascii=False))
            self.assertEqual(len(last['meta']['attachments']),tg_module.RICH_MEDIA_MAX)

    @async_test
    async def test_album_is_one_turn_and_http_upload_download_private(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True)
            app=env.app;env.script([{'text':'Увидел альбом.'}])
            for n in range(2):
                env.fake.state.tg_files[str(n)]={'file_path':'files/'+str(n),'raw':PNG}
                await app.telegram._handle({'message':{'message_id':100+n,'media_group_id':'album','from':{'id':42},'chat':{'id':42,'type':'private'},'photo':[{'file_id':str(n)}]}})
            await self.until(lambda:self.sent(env))
            users=[r for r in app.store.messages(env.sid) if r['role']=='user']
            self.assertEqual(len(users),1)
            self.assertEqual(len(users[0]['meta']['attachments']),2)
            self.assertTrue(users[0]['meta']['untrusted'])
            server=HttpServer(create_web(app,allowed_hosts={'127.0.0.1'}));await server.start('127.0.0.1',0)
            try:
                url=f'http://127.0.0.1:{server.port}'
                response=await app.http.post(url+'/api/attachments',headers=owner_headers(**{'X-Bender':'1'}),json={'name':'../../note.txt','data':base64.b64encode(b'hello').decode()})
                async with response:item=json.loads(await response.read())
                self.assertEqual(response.status,200,item)
                self.assertEqual(item['name'],'note.txt')
                response=await app.http.get(url+'/api/attachments/'+item['id'],headers=owner_headers())
                async with response:self.assertEqual(await response.read(),b'hello')
                self.assertIn('attachment;',response.headers.get('Content-Disposition',''))
                child=app.agents.create('Другой',awaken=False)
                response=await app.http.get(url+'/api/attachments/'+item['id'],headers=owner_headers(**{'X-Bender-Agent':child['id']}))
                async with response:await response.read()
                self.assertEqual(response.status,404)
            finally:await server.stop()
