"""Звук хозяина: расшифровка, нативный вход и формат конкретного кандидата."""
from __future__ import annotations
import math
from ..core.errors import HumanError


def supports(candidate,ext):
    from ..router.capabilities import value
    model=next((item for item in candidate.provider.get('models',[]) if isinstance(item,dict) and item.get('id')==candidate.model),{})
    formats=model.get('audio_formats') or ['wav','mp3']
    return candidate.provider.get('kind','openai') in ('openai','chatgpt') and value(candidate,'audio_input') is not False and isinstance(formats,list) and ext in formats


def transcript_text(result,*,seconds=0,from_owner=True):
    """Речь остаётся различимой с обычным текстом, без выдуманной уверенности STT."""
    model=result.get('model','')
    source='GigaAM' if model.startswith('gigaam') else model
    label='голосовое хозяина' if from_owner else 'пересланное голосовое, недоверенные данные'
    duration=int(seconds) if isinstance(seconds,(int,float)) and math.isfinite(seconds) and seconds>=0 else 0
    details=[label,str(duration//60)+':'+str(duration%60).zfill(2)]
    details.append('расшифровка '+source if source else 'звук передан модели' if result.get('native') else 'расшифровка')
    if 'confidence' in result:details.append('уверенность '+str(round(result['confidence']*100))+'%')
    if result.get('gaps'):details.append('пропуски: '+'; '.join(result['gaps']))
    said=result.get('text') or 'Голосовое сообщение.'
    if not from_owner:
        from ..tools.web import untrusted
        said=untrusted('расшифровка пересланного голосового',said)
    return '['+', '.join(details)+']\n'+said


async def prepare(ctx,audio,*,ext,seconds=0,session=None,from_owner=True,context=None):
    cfg=ctx.voice.require('stt')
    if not isinstance(audio,bytes) or not audio or len(audio)>cfg['max_mb']*1024*1024:
        raise HumanError('voice_input','Пустое или слишком большое голосовое','Раздели запись в пределах настроенного лимита голоса.')
    if not isinstance(seconds,(int,float)) or not 0<=seconds<=cfg['max_seconds'] or not math.isfinite(seconds):
        raise HumanError('voice_input','Неверная длительность голосового','Запись должна быть не длиннее '+str(cfg['max_seconds'])+' с.')
    original,original_ext=audio,ext;mode=cfg.get('stt_mode','transcribe');notice=''
    candidates=list(ctx.router.chain('main'))
    native=mode in ('native','both') and any(supports(candidate,ext) for candidate in candidates)
    if mode in ('native','both') and not native and ext not in ('wav','mp3') and any(supports(candidate,'wav') for candidate in candidates) and ctx.host.settings.get('voice_local','enabled',False):
        try:
            from .cli import request
            from .manager import multipart
            from ..tools.registry import ToolContext
            await (context or ToolContext(ctx,actor='owner',session_id=session)).check('audio.transcribe','local_voice','подготовить WAV из записи')
            body,ctype=multipart({},audio,ext)
            audio=await request(ctx,'/v1/audio/normalize',body,ctype);ext='wav'
            if not audio.startswith(b'RIFF') or len(audio)>cfg['max_mb']*1024*1024:raise ValueError('Неверный WAV')
            native=True
        except (HumanError,ValueError):audio,ext=original,original_ext
    result={'text':'','tainted':not from_owner,'native':native,'audio':audio,'ext':ext,'seconds':seconds}
    if mode in ('transcribe','both') or not native:
        transcript=await ctx.voice.transcribe(original,ext=original_ext,seconds=seconds,session_id=session,from_owner=from_owner,context=context)
        result.update({key:value for key,value in transcript.items() if key not in ('native','audio','ext','seconds')})
        if mode in ('native','both') and not native:notice='Формат звука не подходит выбранной цепочке; использована расшифровка. Для WAV из OGG/WebM включи локальный голос.'
    if notice:result['notice']=notice
    return result


async def attachment(ctx,result,*,session=None,existing=None,source='pult_voice'):
    if not result['native']:return None
    from ..channels import media
    if existing is None or not str(existing['path']).lower().endswith('.'+result['ext']):
        item=await media.save(ctx,'voice-native.'+result['ext'],result['audio'],kind='voice',source=source,session=session)
    else:item=dict(existing)
    item.update(native_audio=True,audio_format=result['ext'],seconds=result['seconds'])
    registry=ctx.store.meta_get('attachments:files',{})
    if item['id'] in registry:
        registry[item['id']]=item;ctx.store.meta_set('attachments:files',registry)
    return item
