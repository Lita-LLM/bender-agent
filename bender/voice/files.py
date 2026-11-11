"""Аудиофайл из вложений: ограниченная длительность и Уши, всегда недоверенные данные."""
from pathlib import Path
from ..core.errors import HumanError
from ..core.pathutil import within

EXTENSIONS={'mp3','ogg','opus','wav','m4a','flac','webm'}


def format_of(item):
    ext=Path(item.get('name') or item['path']).suffix.lower().lstrip('.')
    if ext in EXTENSIONS:return 'ogg' if ext=='opus' else ext
    return {'audio/wav':'wav','audio/x-wav':'wav','audio/ogg':'ogg','audio/opus':'ogg','audio/mpeg':'mp3','audio/flac':'flac','audio/x-flac':'flac','audio/mp4':'m4a'}.get(item.get('mime'))


async def prepare(agent_ctx,item,*,session=None):
    from .input import attachment
    cfg=agent_ctx.voice.require('stt');path=Path(item['path'])
    if not within(path,agent_ctx.dirs.work):raise HumanError('audio_file','Аудиофайл вне рабочего пространства','Прочитай его обычным файловым тулом с проверкой доступа.')
    from ..tools.registry import ToolContext
    await ToolContext(agent_ctx,actor='owner',session_id=session).check('audio.transcribe',item.get('name','audio'),'обработать присланный аудиофайл')
    with path.open('rb') as source:raw=source.read(cfg['max_mb']*1024*1024+1)
    if not raw or len(raw)>cfg['max_mb']*1024*1024:raise HumanError('audio_file','Аудиофайл пустой или больше лимита голоса','Сократи запись; исходный файл сохранён.')
    result=await decode(agent_ctx,raw,format_of(item),session=session)
    seconds=result['seconds']
    native=await attachment(agent_ctx,result,session=session,existing=item,source='telegram')
    label='[Аудиофайл, недоверенные данные, '+str(int(seconds//60))+':'+str(int(seconds%60)).zfill(2)
    label+=', расшифровка '+str(result.get('model')) if result.get('model') else ', звук передан модели'
    from ..tools.web import untrusted
    return {'text':label+']\n'+untrusted('расшифровка аудиофайла',result.get('text') or 'Аудиозапись.'),'item':native or item,'tainted':True,'notice':result.get('notice','')}


async def decode(agent_ctx,raw,ext,*,session=None,context=None):
    """Разобрать уже разрешённые байты; не читает пути и не повышает права вызывающего."""
    from . import duration
    from .input import prepare as input_prepare
    from ..tools.registry import ToolContext
    cfg=agent_ctx.voice.require('stt')
    if not raw or len(raw)>cfg['max_mb']*1024*1024:raise HumanError('audio_file','Аудиофайл пустой или больше лимита голоса','Сократи запись; исходный файл сохранён.')
    if raw.startswith(b'RIFF') and raw[8:12]==b'WAVE':ext='wav'
    elif raw.startswith(b'OggS'):ext='ogg'
    elif raw.startswith(b'fLaC'):ext='flac'
    elif raw.startswith(b'ID3') or raw[:1]==b'\xff':ext='mp3'
    elif raw[4:8]==b'ftyp':ext='m4a'
    if not ext:raise HumanError('audio_file','Не удалось определить формат аудиофайла','Пришли WAV, MP3, OGG/Opus, M4A или FLAC.')
    seconds=duration.seconds(raw,ext)
    if seconds is not None and seconds>cfg['max_seconds']:raise HumanError('audio_length','Запись длиннее лимита голоса','Раздели её на записи до '+str(cfg['max_seconds'])+' с; исходный файл сохранён.')
    if seconds is None:
        if not agent_ctx.host.settings.get('voice_local','enabled',False):
            raise HumanError('audio_duration','Не удалось проверить длительность этой записи','Пришли WAV/MP3/OGG/M4A/FLAC с читаемыми заголовками или включи локальный голос для декодирования. Исходный файл сохранён.')
        from .cli import request
        from .manager import multipart
        await (context or ToolContext(agent_ctx,actor='owner',session_id=session)).check('audio.transcribe','local_voice','проверить длительность записи локальным декодером')
        body,ctype=multipart({},raw,ext);raw=await request(agent_ctx,'/v1/audio/normalize',body,ctype);ext='wav';seconds=duration.seconds(raw,ext)
        if seconds is None or seconds>cfg['max_seconds']:raise HumanError('audio_length','Декодер не подтвердил допустимую длительность записи','Пришли короткий WAV; исходный файл сохранён.')
    return await input_prepare(agent_ctx,raw,ext=ext,seconds=seconds,session=session,from_owner=False,context=context)
