"""Картинка файлом («без сжатия») доходит модели как фото; большая — честная подсказка; гифка качается один раз."""
import base64
import json
from pathlib import Path
from unittest import mock

from bender.channels import media
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv
from tests import test_telegram as original
from tests import test_matrix

PNG=b'\x89PNG\r\n\x1a\n'+b'\0'*20
BIG_PNG=b'\x89PNG\r\n\x1a\n'+b'\0'*(6*1024*1024)
SVG=b'<svg xmlns="http://www.w3.org/2000/svg"/>'


class ImageDocumentTest(TempDirCase):
    _up=original.TelegramTest._up
    until=original.TelegramTest.until
    sent=original.TelegramTest.sent

    def last_model_body(self,env):
        body=[r for r in env.fake.state.requests if r.get('api')=='openai'][-1]['body']['messages'][-1]['content']
        return [p for p in body if isinstance(p,dict) and p.get('type')=='image_url'],json.dumps(body,ensure_ascii=False)

    async def send(self,env,mid,answer,**part):
        tg=env.app.telegram
        env.script([{'text':answer}])
        await tg._handle({'message':{'message_id':mid,'from':{'id':42},'chat':{'id':42,'type':'private'},**part}})
        await self.until(lambda:any(m.get('text')==answer for m in self.sent(env)))
        await self.until(lambda:not tg._bg)

    @async_test
    async def test_document_png_reaches_model_as_image_big_and_svg_get_hint(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);st=env.fake.state
            st.tg_files.update({'png':{'file_path':'files/scan','raw':PNG},'big':{'file_path':'files/big','raw':BIG_PNG},
                                'svg':{'file_path':'files/logo.svg','raw':SVG}})
            # Имя без расширения: картинку узнаём по содержимому, а не по имени или заявленному mime.
            await self.send(env,1,'Вижу скан.',document={'file_id':'png','file_name':'скан','mime_type':'application/octet-stream'},caption='что тут?')
            shots,text=self.last_model_body(env)
            self.assertEqual(len(shots),1);self.assertIn('image/png',shots[0]['image_url']['url'])
            self.assertIn('Вложение (недоверенные данные): document, скан',text);self.assertNotIn('не приложена',text)
            await self.send(env,2,'Большая.',document={'file_id':'big','file_name':'big.png','mime_type':'image/png'})
            shots,text=self.last_model_body(env)
            self.assertEqual(shots,[]);self.assertIn('картинка больше 3,75 МБ',text);self.assertIn('read_file',text)
            await self.send(env,3,'Вектор.',document={'file_id':'svg','file_name':'logo.svg','mime_type':'image/svg+xml'})
            shots,text=self.last_model_body(env)
            self.assertEqual(shots,[]);self.assertIn('формат image/svg+xml модель не принимает',text)
            # read_file не обходит предел: огромную картинку модели не прикладывает.
            path=next(Path(v['path']) for v in env.app.store.meta_get('attachments:files',{}).values() if v['name']=='big.png')
            read=await env.app.tools.run('read_file',{'path':str(path)},ToolContext(env.app,session_id=env.sid))
            self.assertFalse(read.ok);self.assertIn('3,75 МБ',read.content)

    @async_test
    async def test_photo_description_unchanged_and_size_rule_same_for_photo(self):
        async with MindEnv(self.tmp) as env:
            small=env.app.dirs.work/'small.png';small.write_bytes(PNG)
            large=env.app.dirs.work/'large.png';large.write_bytes(BIG_PNG)
            photo=media.register(env.app,small,kind='photo',source='pult')
            self.assertEqual(media.describe([photo]),f"Вложение (недоверенные данные): photo, small.png, {len(PNG)} байт; путь: {small}")
            parts=media.content('Смотри',[photo])
            self.assertEqual([p['type'] for p in parts],['text','image'])
            # Пульт помечает image/* как photo: большой снимок не уходит модели и там, с той же подсказкой.
            parts=media.content('Смотри',[media.register(env.app,large,kind='photo',source='pult')])
            self.assertEqual([p['type'] for p in parts],['text']);self.assertIn('картинка больше 3,75 МБ',parts[0]['text'])
            self.assertLessEqual(len(base64.b64encode(b'\0'*media.IMAGE_RAW_LIMIT)),media.IMAGE_LIMIT,'предел считается по base64')
            # Выключенная передача картинок — без приложения, как раньше.
            self.assertEqual([p['type'] for p in media.content('Смотри',[photo],vision=False)],['text'])

    @async_test
    async def test_animation_with_duplicate_document_downloads_once(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env,owner=True);st=env.fake.state
            st.tg_files['gif']={'file_path':'files/a.mp4','raw':b'\0\0\0\x18ftypmp42'+b'\0'*20}
            info={'file_id':'gif','file_unique_id':'u','file_name':'a.mp4','mime_type':'video/mp4','duration':1}
            items=await env.app.telegram.receive_media(env.app,[{'animation':dict(info),'document':dict(info)}],env.sid)
            self.assertEqual(st.tg_getfile,['gif'])
            self.assertEqual([i['kind'] for i in items],['animation'])
            # Документ без анимации качается как прежде.
            items=await env.app.telegram.receive_media(env.app,[{'document':dict(info)}],env.sid)
            self.assertEqual([i['kind'] for i in items],['document']);self.assertEqual(st.tg_getfile,['gif','gif'])


class MatrixImageFileTest(TempDirCase):
    setup_matrix=test_matrix.MatrixTest.setup_matrix
    shutdown=test_matrix.MatrixTest.shutdown
    event=test_matrix.MatrixTest.event
    pair=test_matrix.MatrixTest.pair

    @async_test
    async def test_matrix_file_with_png_is_attached_as_image(self):
        await self.setup_matrix()
        try:
            ctx,sid=await self.pair();self.payload=PNG
            self.events=[self.event('скан',eid='$img',content={'msgtype':'m.file','body':'скан','url':'mxc://example.org/img_1'})]
            with mock.patch.object(ctx.mind,'say',new=mock.AsyncMock()) as say:await self.app.matrix.poll_once()
            parts=say.call_args.kwargs['content']
            self.assertEqual([p['type'] for p in parts],['text','image']);self.assertEqual(parts[1]['media_type'],'image/png')
            ctx.settings.set('attachments',{'vision':False})
            self.events=[self.event('скан',eid='$img2',content={'msgtype':'m.file','body':'скан','url':'mxc://example.org/img_2'})]
            with mock.patch.object(ctx.mind,'say',new=mock.AsyncMock()) as say:await self.app.matrix.poll_once()
            self.assertEqual([p['type'] for p in say.call_args.kwargs['content']],['text'],'выключенная передача картинок действует и в Matrix')
        finally:await self.shutdown()
