"""Длительность берётся из файла, документ/музыка не становятся доверенной командой."""
import asyncio
import io
import json
import struct
import wave
from pathlib import Path
from unittest.mock import patch

from bender.channels import video_in
from bender.voice.duration import seconds
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv
from tests import test_telegram as telegram


def wav(duration=1):
    output=io.BytesIO()
    with wave.open(output,'wb') as file:
        file.setnchannels(1);file.setsampwidth(2);file.setframerate(8000);file.writeframes(b'\0\0'*int(duration*8000))
    return output.getvalue()


class AudioFilesTest(TempDirCase):
    until=telegram.TelegramTest.until
    sent=telegram.TelegramTest.sent

    def test_real_encoded_fixtures_do_not_need_decoder_in_core(self):
        folder=Path(__file__).with_name('fixtures')/'audio'
        for ext,expected in [('mp3',2.088),('ogg',2),('flac',2),('m4a',2)]:
            with self.subTest(ext=ext):self.assertAlmostEqual(seconds((folder/('two-seconds.'+ext)).read_bytes(),ext),expected,places=5)

    def test_header_duration_formats_and_malformed_inputs(self):
        self.assertEqual(seconds(wav(2),'wav'),2)
        frame=b'\xff\xfb\x90\0'+b'\0'*413
        self.assertAlmostEqual(seconds(frame*100,'mp3'),100*1152/44100)
        flac=b'fLaC\x80\0\0\x22'+b'\0'*10+((16000<<44)|32000).to_bytes(8,'big')+b'\0'*16
        self.assertEqual(seconds(flac,'flac'),2)
        def page(body,granule,flags=0):
            return b'OggS\0'+bytes([flags])+struct.pack('<QII',granule,1,0)+b'\0'*4+bytes([1,len(body)])+body
        opus=b'OpusHead'+b'\x01\x01'+struct.pack('<H',312)+struct.pack('<I',48000)+b'\0'*3
        self.assertEqual(seconds(page(opus,0,2)+page(b'\0',96312,4),'ogg'),2)
        def atom(kind,payload):return struct.pack('!I',len(payload)+8)+kind+payload
        mdhd=atom(b'mdhd',b'\0'*12+struct.pack('!II',1000,2000)+b'\0'*4)
        hdlr=atom(b'hdlr',b'\0'*8+b'soun'+b'\0'*12)
        m4a=atom(b'ftyp',b'M4A \0\0\0\0')+atom(b'moov',atom(b'trak',atom(b'mdia',mdhd+hdlr)))
        self.assertEqual(seconds(m4a,'m4a'),2)
        for raw in [b'',b'OggS',b'fLaC',b'ID3',m4a[:-12],frame[:-1],b'RIFF\0\0\0\0WAVE']:
            self.assertIsNone(seconds(raw))

    @async_test
    async def test_document_wav_and_audio_message_are_transcribed_without_restart(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            app.models.set_role('ears',[{'provider':pid,'model':'ears'}]);app.settings.set('voice',{'enabled':True})
            await telegram.TelegramTest._up(self,env,owner=True)
            cases=[('document','wav',wav()),('audio','wav',wav())]
            folder=Path(__file__).with_name('fixtures')/'audio'
            cases += [('document',ext,(folder/('two-seconds.'+ext)).read_bytes()) for ext in ('mp3','ogg','flac','m4a')]
            for index,(kind,ext,raw) in enumerate(cases):
                file_id='wave-'+str(index);env.fake.state.tg_files[file_id]={'file_path':'files/'+file_id+'.'+ext,'raw':raw}
                answer='Запись разобрана '+str(index);env.script([{'text':answer}])
                msg={'message_id':1200+index,'from':{'id':42},'chat':{'id':42,'type':'private'},
                     kind:{'file_id':file_id,'file_name':'запись.'+ext,'duration':1}}
                await app.telegram._handle({'message':msg})
                await self.until(lambda:any(m.get('text')==answer for m in self.sent(env)))
                await self.until(lambda:not app.telegram._bg)
            users=[m for m in app.store.messages(env.sid) if m['role']=='user']
            self.assertEqual(len(users),6)
            for message in users:
                self.assertTrue(message['meta']['untrusted']);self.assertIn('Аудиофайл, недоверенные данные',json.dumps(message['content'],ensure_ascii=False))
                self.assertIn('Привет голосом',json.dumps(message['content'],ensure_ascii=False))
            self.assertEqual(app.store.one("SELECT count(*) FROM calls WHERE role='ears'")[0],6)

    @async_test
    async def test_declared_short_duration_does_not_hide_long_file_and_video_has_reason(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            app.models.set_role('ears',[{'provider':pid,'model':'ears'}]);app.settings.set('voice',{'enabled':True,'max_seconds':1})
            await telegram.TelegramTest._up(self,env,owner=True)
            env.fake.state.tg_files['long']={'file_path':'files/long.wav','raw':wav(2)}
            env.script([{'text':'Запись слишком длинная.'}])
            await app.telegram._handle({'message':{'message_id':1203,'from':{'id':42},'chat':{'id':42,'type':'private'},'audio':{'file_id':'long','file_name':'long.wav','duration':1}}})
            await self.until(lambda:any(m.get('text')=='Запись слишком длинная.' for m in self.sent(env)))
            await self.until(lambda:not app.telegram._bg)
            self.assertEqual(app.store.one("SELECT count(*) FROM calls WHERE role='ears'")[0],0)
            user=next(m for m in app.store.messages(env.sid) if m['role']=='user')
            self.assertIn('длиннее лимита',json.dumps(user['content'],ensure_ascii=False))
            env.fake.state.tg_files['clip']={'file_path':'files/clip.mp4','raw':b'video-fixture'}
            env.script([{'text':'Для видео нужна аудиодорожка.'}])
            await app.telegram._handle({'message':{'message_id':1204,'from':{'id':42},'chat':{'id':42,'type':'private'},'video_note':{'file_id':'clip','duration':1}}})
            await self.until(lambda:any(m.get('text')=='Для видео нужна аудиодорожка.' for m in self.sent(env)))
            await self.until(lambda:not app.telegram._bg)
            user=[m for m in app.store.messages(env.sid) if m['role']=='user'][-1]
            text=json.dumps(user['content'],ensure_ascii=False);self.assertIn('Файл:',text)
            self.assertIn('Кадры не получились' if video_in.ffmpeg() else 'нет ffmpeg',text)
            self.assertTrue(user['meta']['untrusted'])
            self.assertTrue(any('Кружок' in m.get('text','') for m in self.sent(env)))  # хозяин знает, что ролик не разобран
            with patch('bender.channels.video_in.ffmpeg',lambda:None):
                env.script([{'text':'Без ffmpeg.'}])
                await app.telegram._handle({'message':{'message_id':1205,'from':{'id':42},'chat':{'id':42,'type':'private'},'video_note':{'file_id':'clip','duration':1}}})
                await self.until(lambda:any(m.get('text')=='Без ffmpeg.' for m in self.sent(env)))
                await self.until(lambda:not app.telegram._bg)
            user=[m for m in app.store.messages(env.sid) if m['role']=='user'][-1]
            self.assertIn('нет ffmpeg',json.dumps(user['content'],ensure_ascii=False))
            self.assertTrue(any('apt install ffmpeg' in m.get('text','') for m in self.sent(env)))
