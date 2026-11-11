"""Необязательный голосовой процесс: GigaAM/Piper, только localhost и свой ключ.

Запускается Python из отдельного venv. Импорты numpy/onnx_asr/piper/soundfile
происходят только в Engine; ядро Бендера этих зависимостей не загружает.
"""
from __future__ import annotations
import argparse
import hmac
import io
import json
from pathlib import Path
import threading
import socket
import selectors
import wave
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from email import policy
from email.parser import BytesParser

LIMIT=25*1024*1024
import hashlib
CODE=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]  # Бендер сверяет: служба на свежем коде после обновления?


class Engine:
    def __init__(self,folder,threads=2):
        self.folder=Path(folder);self.threads=threads;self.asr=None;self.tts=None;self.voices={}
        self.manifest=json.loads((self.folder/'installed.json').read_text())

    def installed(self):
        """Голоса пакета; старый манифест знал один голос."""
        own=self.manifest.get('voices')
        if isinstance(own,dict) and own:return own
        return {self.manifest.get('voice') or 'default':{'tts_file':self.manifest['tts_file'],'espeak_dir':self.manifest.get('espeak_dir')}}

    def pick(self,voice):
        """Голос агента; неизвестный — по умолчанию. Новый голос виден без перезапуска службы."""
        if voice and voice not in self.installed():
            try:self.manifest=json.loads((self.folder/'installed.json').read_text())
            except (OSError,ValueError):pass
        voices=self.installed()
        return voice if voice in voices else self.manifest.get('voice') if self.manifest.get('voice') in voices else next(iter(voices))

    def normalize(self,raw):
        import numpy as np
        import soundfile as sf
        with sf.SoundFile(io.BytesIO(raw)) as source:
            if len(source)/source.samplerate>600:raise ValueError('Запись длиннее 10 минут')
            samples=source.read(dtype='float32',always_2d=True).mean(axis=1);rate=source.samplerate
        if rate!=16000:
            samples=np.interp(np.arange(int(len(samples)*16000/rate))*rate/16000,np.arange(len(samples)),samples)
        pcm=(np.clip(samples,-1,1)*32767).astype('<i2').tobytes();output=io.BytesIO()
        with wave.open(output,'wb') as target:
            target.setnchannels(1);target.setsampwidth(2);target.setframerate(16000);target.writeframes(pcm)
        return output.getvalue()

    def opus(self,raw):
        import numpy as np
        import soundfile as sf
        with sf.SoundFile(io.BytesIO(raw)) as source:
            if len(source)/source.samplerate>600:raise ValueError('Озвучка длиннее 10 минут')
            samples=source.read(dtype='float32',always_2d=True).mean(axis=1);rate=source.samplerate
        if rate!=48000:samples=np.interp(np.arange(int(len(samples)*48000/rate))*rate/48000,np.arange(len(samples)),samples)
        output=io.BytesIO();sf.write(output,samples,48000,format='OGG',subtype='OPUS')
        return output.getvalue()

    def transcribe(self,raw):
        import numpy as np
        import soundfile as sf
        import onnx_asr
        import onnxruntime as ort
        with sf.SoundFile(io.BytesIO(raw)) as source:
            if len(source)/source.samplerate>600:raise ValueError('Голосовое длиннее 10 минут; раздели его.')
            samples=source.read(dtype='float32',always_2d=True).mean(axis=1)
            rate=source.samplerate
        if not len(samples):raise ValueError('В аудио нет звука.')
        if self.asr is None:
            options=ort.SessionOptions();options.intra_op_num_threads=self.threads;options.inter_op_num_threads=1
            self.asr=onnx_asr.load_model('gigaam-v3-e2e-ctc',path=self.folder/'models'/'asr',quantization=self.manifest.get('quantization'),sess_options=options,providers=['CPUExecutionProvider'])
        return {'text':self.asr.recognize(np.asarray(samples,dtype=np.float32),sample_rate=rate)}

    def speak(self,text,fmt,voice=None,speed=1):
        from piper import PiperVoice,SynthesisConfig
        if not isinstance(text,str) or not text.strip() or len(text)>20000:raise ValueError('Нужен текст до 20 000 знаков.')
        if fmt not in ('wav','opus'):raise ValueError('Локальный голос выдаёт WAV или OGG/Opus.')
        if voice is not None and not isinstance(voice,str):raise ValueError('Имя голоса — строка.')
        if isinstance(speed,bool) or not isinstance(speed,(int,float)) or not 0.5<=speed<=2:raise ValueError('Скорость от 0.5 до 2.')
        name=self.pick(voice)
        if name not in self.voices:
            while len(self.voices)>=3:self.voices.pop(next(iter(self.voices)))  # в памяти не больше трёх голосов
            entry=self.installed()[name];espeak=entry.get('espeak_dir')  # голосам сообщества нужен свой словарь произношения
            self.voices[name]=PiperVoice.load(self.folder/'models'/'tts'/entry['tts_file'],**({'espeak_data_dir':self.folder/'models'/'tts'/espeak} if espeak else {}))
        self.tts=self.voices[name]=self.voices.pop(name)
        output=io.BytesIO()
        with wave.open(output,'wb') as target:self.tts.synthesize_wav(text,target,**({'syn_config':SynthesisConfig(length_scale=1/speed)} if speed!=1 else {}))
        raw=output.getvalue()
        if len(raw)>LIMIT:raise ValueError('Озвучка больше 25 МБ; раздели текст.')
        if fmt=='opus':
            raw=self.opus(raw)
        return raw,'audio/ogg' if fmt=='opus' else 'audio/wav'


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,engine,token):
        if address[0] not in ('127.0.0.1','::1'):raise ValueError('Голосовой сервер слушает только localhost.')
        self.engine=engine;self.token=token;self.inference=threading.Lock()
        super().__init__(address,Handler)
        self.wake_read,self.wake_write=socket.socketpair();self.stopping=threading.Event();self.finished=threading.Event()

    def serve_forever(self,poll_interval=None):
        # Сокет пробуждения вместо периодического опроса простаивающей службы.
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self,selectors.EVENT_READ,'request');selector.register(self.wake_read,selectors.EVENT_READ,'stop')
                while not self.stopping.is_set():
                    for key,_ in selector.select():
                        if key.data=='stop':return
                        if not self.stopping.is_set():self._handle_request_noblock()
        finally:self.finished.set()

    def shutdown(self):
        self.stopping.set()
        try:self.wake_write.send(b'1')
        except OSError:pass
        self.finished.wait(10)

    def server_close(self):
        super().server_close();self.wake_read.close();self.wake_write.close()


class Handler(BaseHTTPRequestHandler):
    server_version='BenderVoice/1'
    def log_message(self,*args):pass  # Текст, ключ и голос не пишутся в access log.

    def send(self,status,value,mime='application/json'):
        if not isinstance(value,bytes):value=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(value)));self.send_header('Cache-Control','no-store');self.end_headers()
        try:self.wfile.write(value)
        except (BrokenPipeError,ConnectionResetError):pass

    def authorized(self):
        host=(self.headers.get('Host') or '').split(':',1)[0]
        ok=host in ('127.0.0.1','localhost') and not self.headers.get('Origin') and hmac.compare_digest(self.headers.get('Authorization','').encode(),('Bearer '+self.server.token).encode())
        if not ok:self.send(403,{'error':{'message':'Локальный голос доступен Бендеру с собственным ключом.'}})
        return ok

    def do_GET(self):
        if not self.authorized():return
        if self.path=='/health':self.send(200,{'ok':True,'code':CODE,'models':['gigaam-v3-e2e-ctc','piper-ru'],'loaded':bool(getattr(self.server.engine,'asr',None) or getattr(self.server.engine,'tts',None))})
        elif self.path=='/v1/models':self.send(200,{'object':'list','data':[{'id':name,'object':'model'} for name in ('gigaam-v3-e2e-ctc','piper-ru')]})
        else:self.send(404,{'error':{'message':'Нет такого метода.'}})

    def do_POST(self):
        self.connection.settimeout(60)
        if not self.authorized():return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=LIMIT:self.send(413,{'error':{'message':'Аудиозапрос должен быть от 1 байта до 25 МБ.'}});return
            raw=self.rfile.read(size)
            if len(raw)!=size:raise ValueError('Запрос оборвался; повтори отправку.')
            if self.path=='/shutdown':
                self.send(200,{'stopping':True});self.server.shutdown();return
            if self.path in ('/v1/audio/transcriptions','/v1/audio/normalize','/v1/audio/encode-opus'):
                ctype=self.headers.get('Content-Type','')
                if not ctype.startswith('multipart/form-data;'):raise ValueError('Для распознавания нужен multipart с файлом.')
                parsed=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+ctype+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
                files=[part.get_payload(decode=True) for part in parsed.iter_parts() if part.get_param('name',header='content-disposition')=='file']
                if len(files)!=1 or not files[0]:raise ValueError('Нужен один непустой аудиофайл.')
                with self.server.inference:
                    result=self.server.engine.normalize(files[0]) if self.path.endswith('/normalize') else self.server.engine.opus(files[0]) if self.path.endswith('/encode-opus') else self.server.engine.transcribe(files[0])
                self.send(200,result,'audio/wav' if self.path.endswith('/normalize') else 'audio/ogg' if self.path.endswith('/encode-opus') else 'application/json')
            elif self.path=='/v1/audio/speech':
                data=json.loads(raw)
                if not isinstance(data,dict):raise ValueError('Нужен JSON с текстом input.')
                with self.server.inference:audio,mime=self.server.engine.speak(data.get('input'),data.get('response_format','wav'),data.get('voice'),data.get('speed',1))
                self.send(200,audio,mime)
            else:self.send(404,{'error':{'message':'Нет такого метода.'}})
        except (ValueError,KeyError,TypeError):self.send(400,{'error':{'message':'Не удалось разобрать аудио или параметры. Пришли WAV/OGG и допустимый текст; локальный голос выдаёт WAV или Opus.'}})
        except Exception:self.send(503,{'error':{'message':'Локальные модели не готовы. Проверь bender voice install и службу; подробности входного аудио не записываются.'}})


def main(argv=None):
    parser=argparse.ArgumentParser(description='Необязательный локальный голос Бендера')
    parser.add_argument('--folder',required=True);parser.add_argument('--port',type=int,default=8899);parser.add_argument('--threads',type=int,default=2)
    args=parser.parse_args(argv);folder=Path(args.folder)
    token=(folder/'token').read_text().strip()
    if not token or not 1<=args.threads<=32:raise ValueError('Проверь ключ голосового процесса и число потоков.')
    server=Server(('127.0.0.1',args.port),Engine(folder,args.threads),token)
    try:server.serve_forever(poll_interval=60)
    except KeyboardInterrupt:pass
    finally:server.server_close()
    return 0


if __name__=='__main__':raise SystemExit(main())
