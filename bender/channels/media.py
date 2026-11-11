"""Вложения каналов: только своя папка, ограниченный размер, недоверенные данные."""
from __future__ import annotations
import base64
import datetime
import json
import mimetypes
import re
import secrets
from pathlib import Path
from ..core.errors import HumanError
from ..core.fsutil import atomic_write
from ..core.pathutil import within, relative_path
from ..tools.registry import ToolContext

DOWNLOAD_LIMIT=20*1024*1024
UPLOAD_LIMIT=50*1024*1024
# Картинка модели: Anthropic и Claude Code отвергают больше 5 МБ; считаем по base64 — так меряет API, с запасом.
IMAGE_LIMIT=5*1024*1024
IMAGE_RAW_LIMIT=IMAGE_LIMIT//4*3
IMAGES=('image/jpeg','image/png','image/webp','image/gif')


def filename(value):
    from ..tools.web import clean_tokens
    value=clean_tokens(str(value or 'файл').replace('\\','/').rsplit('/',1)[-1])
    value=re.sub(r'[\x00-\x1f\x7f:<>|?*\[\]‹›]','',value).strip('. ')
    value=value[:160]
    # Имя в файловой системе — до 255 байт: с приставкой и временным суффиксом записи держим запас.
    while len(value.encode('utf-8'))>180:
        stem,dot,ext=value.rpartition('.')
        value=(stem[:-1]+dot+ext) if dot and stem and len(ext)<=10 else value[:-1]
    return value.strip('. ') or 'файл'


def mime(raw, name):
    if raw.startswith(b'\xff\xd8\xff'):return 'image/jpeg'
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):return 'image/png'
    if raw.startswith((b'GIF87a',b'GIF89a')):return 'image/gif'
    if raw[:4]==b'RIFF' and raw[8:12]==b'WEBP':return 'image/webp'
    if raw[:4]==b'RIFF' and raw[8:12]==b'WAVE':return 'audio/wav'
    if raw.startswith(b'fLaC'):return 'audio/flac'
    if raw.startswith(b'ID3') or len(raw)>1 and raw[0]==255 and raw[1]&224==224:return 'audio/mpeg'
    if raw.startswith(b'OggS') and (b'OpusHead' in raw[:100] or b'\x01vorbis' in raw[:100]):return 'audio/ogg'
    if raw[4:8]==b'ftyp' and raw[8:12] in (b'M4A ',b'M4B '):return 'audio/mp4'
    return mimetypes.guess_type(name)[0] or 'application/octet-stream'


def require(ctx):
    if not ctx.settings.get('attachments','enabled',True):
        raise HumanError('attachments_off','Вложения выключены','Включи их в настройках «Вложения».')


def original_name(ctx,path):
    from ..core.pathutil import same
    prior=next((item for item in ctx.store.meta_get('attachments:files',{}).values() if same(path,item['path'])),None)
    return prior['name'] if prior else Path(path).name


def register(ctx, path, *, name=None, kind='document', source='agent'):
    require(ctx)
    if not within(path,ctx.dirs.work) or not Path(path).is_file():raise HumanError('attachment_path','Файл вне рабочей папки','Сохрани его в своей рабочей папке.')
    key=secrets.token_hex(16)
    with Path(path).open('rb') as handle:raw=handle.read(UPLOAD_LIMIT+1)
    if len(raw)>UPLOAD_LIMIT:raise HumanError('attachment_size','Файл больше 50 МБ','Раздели его перед отправкой.')
    item={'id':key,'path':str(path),'name':filename(name or Path(path).name),'size':len(raw),'mime':mime(raw,str(path)),'kind':kind,'source':source}
    records=ctx.store.meta_get('attachments:files',{})
    from ..core.pathutil import same
    prior=next((i for i in records.values() if i.get('source')!='agent' and same(path,i['path'])),None)
    if prior and source=='agent':item['source']=prior['source']
    records[key]=item;ctx.store.meta_set('attachments:files',dict(list(records.items())[-2000:]))
    return item


async def save(ctx, name, raw, *, kind='document', source='pult', session=None, actor='owner', why='сохранить присланное хозяином вложение', unattended=False, tainted=False):
    require(ctx)
    limit=min(DOWNLOAD_LIMIT,ctx.settings.get('attachments','max_mb',20)*1024*1024)
    if not isinstance(raw,bytes) or len(raw)>limit:
        raise HumanError('attachment_size','Пустой или слишком большой файл','Входящие вложения — до 20 МБ и в пределах выбранного лимита.')
    name=filename(name)
    path=ctx.dirs.work/'inbox'/datetime.date.today().isoformat()/(secrets.token_hex(8)+'-'+name)
    # Ссылка на inbox не должна направить загрузку за пределы рабочей папки.
    if not within(path,ctx.dirs.work):raise HumanError('attachment_path','Папка входящих стала ссылкой наружу','Убери ссылку work/inbox и повтори.')
    # Каналы без хозяина рядом (почта в фоне) передают своего actor и unattended: вопрос превращается в отказ, а не в карточку.
    await ToolContext(ctx,actor=actor,session_id=session,unattended=unattended,tainted=tainted).check('fs.write',str(path),why)
    atomic_write(path,raw,durable=False)
    return register(ctx,path,name=name,kind=kind,source=source)


def get(ctx,key):
    require(ctx)
    item=ctx.store.meta_get('attachments:files',{}).get(key)
    if not item or not within(item['path'],ctx.dirs.work) or not Path(item['path']).is_file():
        raise HumanError('attachment_missing','Файл вложения не найден','Он мог быть удалён или относится к другому агенту.',status=404)
    return item


def untrusted(ctx,path):
    from ..core.pathutil import same
    return within(path,ctx.dirs.work/'inbox') or any(i.get('source')!='agent' and same(path,i['path']) for i in ctx.store.meta_get('attachments:files',{}).values())


def sniffed(item):
    """Картинка по байтам файла, а не по имени: документ с чужими байтами (HEIC под .jpg) модели картинкой не отдаём."""
    try:
        with Path(item['path']).open('rb') as f:return mime(f.read(64),'')==item.get('mime')
    except (OSError,KeyError,TypeError):return False


def image_note(item):
    """Почему картинку из фото/файла не приложили модели; пусто — прикладывается."""
    kind=str(item.get('mime') or '')
    if item.get('kind') not in ('photo','document') or not kind.startswith('image/'):return ''
    if kind in IMAGES and item.get('kind')=='document' and not sniffed(item):
        return 'по содержимому файл не похож на JPEG, PNG, GIF или WebP — не приложен картинкой; открой read_file'
    if kind not in IMAGES:return f'формат {kind} модель не принимает картинкой (только JPEG, PNG, GIF, WebP) — не приложена; переведи в PNG и открой read_file'
    if int(item.get('size') or 0)>IMAGE_RAW_LIMIT:return 'картинка больше 3,75 МБ (предел модели — 5 МБ в base64) — не приложена; сожми её и открой read_file'
    return ''


def describe(items):
    lines=[]
    for i in items:
        note=image_note(i)
        lines.append(f"Вложение (недоверенные данные): {i['kind']}, {i['name']}, {i['size']} байт; путь: {i['path']}"+(f'; {note}' if note else ''))
    return '\n'.join(lines)


def content(text, items, *, vision=True,audio=False):
    """Фото и картинка файлом (document с картинкой по содержимому) уходят модели одинаково, до IMAGE_LIMIT."""
    parts=[{'type':'text','text':text+'\n\n'+describe(items)}]
    for item in items:
        if vision and item['kind'] in ('photo','document') and item['mime'] in IMAGES and not image_note(item):
            raw=Path(item['path']).read_bytes()
            if len(raw)<=IMAGE_RAW_LIMIT:parts.append({'type':'image','media_type':item['mime'],'data':base64.b64encode(raw).decode(),'path':item['path']})
        for number,shot in enumerate(item.get('frames') or [] if vision else [],1):
            if shot.get('mime') not in IMAGES or not Path(shot['path']).is_file() or Path(shot['path']).stat().st_size>IMAGE_RAW_LIMIT:continue
            label='Обложка' if shot.get('cover') else 'Кадр '+str(number)+(f" (≈{shot['at']:g} с)" if shot.get('at') is not None else '')
            if len(item['frames'])>1 or shot.get('cover'):parts.append({'type':'text','text':label+' — '+item['name']+':'})
            parts.append({'type':'image','media_type':shot['mime'],'data':base64.b64encode(Path(shot['path']).read_bytes()).decode(),'path':shot['path']})
        if audio and item.get('native_audio'):
            parts.append({'type':'audio','format':item['audio_format'],'data':base64.b64encode(Path(item['path']).read_bytes()).decode(),'seconds':item.get('seconds',0),'path':item['path']})
    return parts


def for_model(messages,candidate,*,audio=True):
    from ..router.capabilities import value as capability
    result=[]
    for message in messages:
        value=message.get('content')
        if isinstance(value,list):
            from ..voice.input import supports
            value=[{'type':'text','text':'Модель не видит картинки. Файл сохранён по пути '+p.get('path','из описания вложения')+'. Используй описание роли Зрение или предложи переключить модель.'} if p.get('type')=='image' and capability(candidate,'vision') is False else
                   {'type':'text','text':'Передача звука выключена или эта модель не принимает данный формат. Используй имеющуюся расшифровку или предложи аудиомодель; не выдумывай содержание записи.'} if p.get('type')=='audio' and (not audio or not supports(candidate,p.get('format'))) else p for p in value]
        result.append({**message,'content':value})
    return result


def multipart(fields, files):
    boundary='bender-'+secrets.token_hex(16);parts=[]
    for key,value in fields.items():
        if isinstance(value,(dict,list,bool)):value=json.dumps(value,ensure_ascii=False)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    for key,item in files.items():
        safe=filename(item['name']).replace('"','_')
        parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{safe}"\r\nContent-Type: {item["mime"]}\r\n\r\n'.encode(),item['bytes'],b'\r\n'])
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts),'multipart/form-data; boundary='+boundary


def outgoing_kind(path,kind='auto'):
    if kind!='auto':return kind
    ext=Path(path).suffix.lower()
    return 'photo' if ext in ('.jpg','.jpeg','.png','.webp') else 'voice' if ext in ('.ogg','.opus') else 'video' if ext in ('.mp4','.mov') else 'audio' if ext in ('.mp3','.m4a','.wav') else 'document'
