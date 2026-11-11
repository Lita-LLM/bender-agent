"""OpenAI-совместимые STT/TTS: свои цепочки, лимиты, Предохранитель и общий счёт."""
from __future__ import annotations

import json
import hashlib
import math
import secrets
import time

from ..core.errors import HumanError
from ..net.http_client import NetError, Timeouts
from ..router.adapters import adapter_for
from ..router.errors import ProviderError, from_http
from ..tools.registry import ToolContext

MIMES = {'webm':'audio/webm','ogg':'audio/ogg','wav':'audio/wav','mp3':'audio/mpeg','m4a':'audio/mp4','mp4':'audio/mp4','flac':'audio/flac'}
FORMATS = {'mp3':'audio/mpeg','opus':'audio/ogg','wav':'audio/wav'}


def error(title, hint='Настройки → Голос; модели → роли «Уши» и «Голос».'):
    return HumanError('voice', title, hint)


def multipart(fields, audio, ext):
    boundary = 'bender-' + secrets.token_hex(16)
    chunks=[]
    for key,value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice.{ext}"\r\nContent-Type: {MIMES[ext]}\r\n\r\n'.encode(),audio,f'\r\n--{boundary}--\r\n'.encode()])
    return b''.join(chunks), 'multipart/form-data; boundary='+boundary


class Voice:
    def __init__(self, agent_ctx):
        self.app=agent_ctx

    def config(self):
        return self.app.settings.group('voice')

    def require(self, kind):
        cfg=self.config()
        if self.app.safe or not cfg['enabled'] or not cfg[kind] or self.app.guard.halted:
            raise error('Голос выключен или агент остановлен')
        return cfg

    async def transcribe(self, audio, *, ext='webm', seconds=0, session_id=None, from_owner=True, context=None):
        cfg=self.require('stt')
        if ext not in MIMES or not isinstance(audio,bytes) or not audio or len(audio)>cfg['max_mb']*1024*1024:
            raise error('Недопустимый или слишком большой аудиофайл', 'Используй WebM, OGG, WAV, MP3 или MP4 в пределах настроенного лимита.')
        if not isinstance(seconds,(int,float)) or not 0<=seconds<=cfg['max_seconds'] or not math.isfinite(seconds):
            raise error('Неверная длительность аудио')
        result=await self._call('ears',audio=audio,ext=ext,seconds=seconds,session_id=session_id,context=context)
        try:
            transcript=json.loads(result.pop('raw').decode('utf-8'));text=transcript.get('text')
            if not isinstance(text,str) or not text.strip() or len(text)>100000:
                raise ValueError('пустая или слишком длинная расшифровка')
        except (ValueError,AttributeError) as exc:
            raise error('Провайдер вернул неверную расшифровку', str(exc)) from None
        result.pop('audio_mime',None)
        confidence=transcript.get('confidence')
        if isinstance(confidence,(int,float)) and not isinstance(confidence,bool) and 0<=confidence<=1:result['confidence']=confidence
        if isinstance(transcript.get('gaps'),list):result['gaps']=[str(value)[:200] for value in transcript['gaps'][:20]]
        return {**result,'text':text,'tainted':not from_owner}

    async def speak(self, text, *, session_id=None, format=None, context=None):
        cfg=self.require('tts')
        if not isinstance(text,str) or not text.strip() or len(text)>20000:
            raise error('Для озвучки нужен текст до 20 000 знаков')
        fmt=format or cfg['format']
        if fmt not in FORMATS:
            raise error('Неподдерживаемый формат голоса')
        result=await self._call('voice',text=text,format=fmt,session_id=session_id,context=context)
        raw=result.pop('raw')
        if not raw:
            raise error('Провайдер вернул пустое аудио')
        if fmt=='opus' and not (raw.startswith(b'OggS') and b'OpusHead' in raw[:4096]):
            raise error('Провайдер не вернул OGG/Opus', 'Telegram получит текст. Перекодирование и ffmpeg не требуются ядру.')
        return {**result,'audio':raw,'mime':result.pop('audio_mime',FORMATS[fmt])}

    async def _call(self, role, *, audio=None,ext=None,seconds=0,text='',format=None,session_id=None,context=None):
        cfg=self.config()
        ctx=context or ToolContext(self.app,actor='owner',session_id=session_id)
        await ctx.check('audio.transcribe' if role=='ears' else 'audio.speak',role,'распознать голос' if role=='ears' else 'озвучить ответ',to_owner=role=='voice')
        candidates=[c for c in self.app.router.chain(role,fallback_main=False) if c.provider.get('kind','openai')=='openai']
        if not candidates:
            raise error('Не настроена аудиоцепочка «'+('Уши' if role=='ears' else 'Голос')+'»')
        last=None
        for candidate in candidates:
            self.require('stt' if role=='ears' else 'tts')
            provider=candidate.provider
            if provider.get('preset')=='local_voice' and not self.app.host.settings.get('voice_local','enabled',False):
                last=error('Локальный голос выключен');continue
            if provider.get('preset')=='local_voice':
                from . import fresh
                await fresh.ensure(self.app)  # после обновления служба могла остаться на старом коде
                wanted=cfg['piper'] or self.app.host.settings.get('voice_local','voice','')
                if role=='voice' and wanted and fresh.missing(self.app,wanted):fresh.fetch(self.app,wanted)
            description=next((model for model in provider.get('models',[]) if isinstance(model,dict) and model.get('id')==candidate.model),{})
            protocol=candidate.params.get('audio_protocol',description.get('audio_protocol',provider.get('audio_protocol',cfg['stt_protocol' if role=='ears' else 'tts_protocol'])))
            if protocol not in (('openai','whisper','gemini') if role=='ears' else ('openai','piper','gemini')):
                raise error('Неверный протокол аудиомодели')
            free=self.app.router.is_free(provider,candidate.model)
            if self.app.body.economy() and not free:
                last=error('Дневной бюджет исчерпан: платное аудио остановлено')
                continue
            price=candidate.params.get('audio_usd_minute' if role=='ears' else 'audio_usd_million_chars',cfg['stt_usd_minute' if role=='ears' else 'tts_usd_million_chars'])
            try:price=float(price)
            except (TypeError,ValueError):price=-1
            if not math.isfinite(price):price=-1
            known=free or (price>=0 and (role!='ears' or seconds>0))
            cost=0 if free or not known else price*(seconds/60 if role=='ears' else len(text)/1000000)
            budget=self.app.body.budget()
            if budget['left'] is not None and cost>max(0,budget['left']):
                last=error('Расчётная цена аудио выше остатка дневного бюджета')
                continue
            for _,key in self.app.router._keys(provider):
                started=time.monotonic()
                headers=adapter_for(provider).headers(key)
                if role=='ears':
                    body,ctype=multipart({'model':candidate.model,'response_format':'json'},audio,ext)
                else:
                    # Локальной службе — свой голос Piper агента, иначе выбранный в настройках; неустановленный служба заменит своим.
                    speaker=cfg['piper'] or self.app.host.settings.get('voice_local','voice','') or None if provider.get('preset')=='local_voice' else cfg['speaker']
                    body=json.dumps({'model':candidate.model,'input':text,**({'voice':speaker} if speaker else {}),**({'speed':cfg['speed']} if cfg['speed']!=1 else {}),'response_format':format},ensure_ascii=False).encode()
                    if protocol=='piper':
                        body=json.dumps({'text':text},ensure_ascii=False).encode()
                    ctype='application/json'
                endpoint=('/inference' if protocol=='whisper' else '/audio/transcriptions') if role=='ears' else ('' if protocol=='piper' else '/audio/speech')
                headers.update({'Content-Type':ctype,'Accept':'application/json' if role=='ears' else 'audio/*'})
                url=provider['base_url'].rstrip('/')+endpoint
                if protocol=='gemini':
                    from . import gemini
                    url,body=gemini.request(provider,candidate.model,role,audio=audio,ext=ext,text=text,speaker=candidate.params.get('speaker',cfg['gemini_speaker']))
                    headers.pop('Authorization',None)
                    if key:headers['x-goog-api-key']=key
                    headers.update({'Content-Type':'application/json','Accept':'application/json'})
                try:
                    async with self.app.router._sem(provider):
                        response=await self.app.http.request('POST',url,body=body,headers=headers,timeouts=Timeouts(connect=15,read=cfg['timeout'],total=cfg['timeout']+15))
                        async with response:
                            raw=await response.read(limit=cfg['max_mb']*1024*1024)
                            if not response.ok:
                                raise from_http(response.status,raw.decode('utf-8','replace'),response.headers,provider=provider.get('name',''),model=candidate.model)
                    audio_mime=response.headers.get('content-type',FORMATS.get(format,'application/json'))
                    if protocol=='gemini':raw,audio_mime=gemini.response(raw,role)
                    if role=='voice' and format=='opus' and not(raw.startswith(b'OggS') and b'OpusHead' in raw[:4096]):
                        if raw.startswith(b'RIFF') and self.app.host.settings.get('voice_local','enabled',False):
                            from .cli import request
                            encoded,encoding_type=multipart({},raw,'wav')
                            raw=await request(self.app,'/v1/audio/encode-opus',encoded,encoding_type);audio_mime='audio/ogg'
                        if not(raw.startswith(b'OggS') and b'OpusHead' in raw[:4096]):
                            raise error('Провайдер не вернул OGG/Opus','Добавь синтез с Opus или установи локальный голос для кодирования PCM. Telegram получит текст.')
                    notice='Аудио: '+('распознавание' if role=='ears' else 'синтез')+(' · цена неизвестна: задай тариф; $0 в счёте не означает бесплатно' if not known else ' · стоимость по настроенному тарифу')
                    self.app.store.add_call(session_id=session_id,role=role,provider=provider.get('name',provider['id']),model=candidate.model,usage={'audio_seconds':seconds,'characters':len(text),'cost_known':known},cost=cost,seconds=time.monotonic()-started,notice=notice,request={'audio_bytes':len(audio or b''),'audio_sha256':hashlib.sha256(audio).hexdigest() if audio else None,'input':text if role=='voice' else None,'format':format})
                    return {'raw':raw,'audio_mime':audio_mime,'cost':cost,'cost_known':known,'notice':notice,'model':candidate.model}
                except (NetError,HumanError,OSError) as exc:
                    last=exc
                    self.app.store.add_call(session_id=session_id,role=role,provider=provider.get('name',''),model=candidate.model,usage={},cost=0,seconds=time.monotonic()-started,notice='Ошибка аудио; возможная цена не сообщена провайдером',request={'audio_bytes':len(audio or b''),'characters':len(text)},error=str(exc))
                    if isinstance(exc,ProviderError) and not exc.failover:
                        break
        raise error('Ни одна аудиомодель не ответила',str(last) if last else 'Проверь цепочку и бюджет.')
