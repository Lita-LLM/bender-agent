"""Части результата MCP для модели: текст — как есть; картинка — файлом в inbox и отдельным сообщением
после результатов тулов; прочие двоичные данные — меткой без base64 (сырой base64 в тексте только тратит контекст)."""
import base64
import binascii
import copy
import json

from ..channels import media
from ..core.errors import HumanError
from ..tools import file_media

EXT={'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp'}


def size_of(data):
    """Примерный размер данных base64 в байтах, без декодирования."""
    if not isinstance(data,str):return 0
    data=data.strip()
    return max(0,len(data)*3//4-data[-2:].count('='))


def omitted(mime,data):
    return f'[двоичные данные {str(mime or "неизвестного типа")[:100]}, {size_of(data)} байт опущены]'


def strip(value):
    """Копия без сырых двоичных полей (blob/data) — для resource и незнакомых частей."""
    value=copy.deepcopy(value)
    def walk(node):
        if isinstance(node,dict):
            for key in list(node):
                if key in ('blob','data') and isinstance(node[key],str) and len(node[key])>64:
                    node[key]=omitted(node.get('mimeType'),node[key])
                else:walk(node[key])
        elif isinstance(node,list):
            for item in node:walk(item)
    walk(value);return value


async def image(ctx,item,number,source):
    """Одна картинка: (метка для текста, часть для модели или None). Ошибки — короткой причиной, без base64."""
    def refused(reason):return f'[картинка {number} не передана: {reason}]',None
    app=ctx.app;data=item.get('data')
    if not app.settings.get('attachments','enabled',True):return refused('вложения выключены в настройках')
    if not app.settings.get('attachments','vision',True):return refused('передача картинок модели выключена в настройках')
    if item.get('mimeType') not in file_media.IMAGES:return refused(f'формат {str(item.get("mimeType"))[:60]} не поддерживается; {size_of(data)} байт опущены')
    try:
        file_media.allowance(ctx,size_of(data))
        try:raw=base64.b64decode(data,validate=True) if isinstance(data,str) else b''
        except (binascii.Error,ValueError):raw=b''
        kind=media.mime(raw[:64],'')
        if not raw or kind not in file_media.IMAGES:return refused('данные повреждены')
        if len(raw)>media.IMAGE_RAW_LIMIT:return refused('картинка больше 3,75 МБ (предел модели — 5 МБ в base64)')
        file_media.allowance(ctx,len(raw))
    except HumanError as exc:return refused(exc.title.lower())
    file_media.spend(ctx,len(raw))  # место в лимите хода занято до записи: параллельные вызовы его не обгонят
    try:
        # Запись — от имени того, кто звал тул (правила хозяина, права агента, стоп-кран), а не от хозяина.
        saved=await media.save(app,f'{source}.{EXT[kind]}',raw,kind='photo',source='mcp',session=ctx.session_id,actor=ctx.actor,
                               unattended=ctx.unattended,tainted=True,why='сохранить картинку из результата MCP '+str(source)[:80])
    except HumanError as exc:
        used=ctx.turn.setdefault('read_media',{'files':0,'bytes':0});used['files']-=1;used['bytes']-=len(raw)
        return refused(exc.title.lower())
    ctx.tainted=True
    part={'type':'image','media_type':kind,'data':base64.b64encode(raw).decode(),'path':saved['path'],'source':'mcp'}
    return f'[картинка {number} приложена отдельным сообщением; файл: {saved["path"]}]',part


async def render(ctx,content,*,origin,attach=True):
    """content — список частей MCP (tools/call). Возвращает (текст, model_parts).
    attach=False (ошибка тула, результат фоновой задачи) — картинки только меткой, без файла и вложения."""
    lines=[];parts=[];number=0
    for item in content:
        if not isinstance(item,dict):continue
        kind=item.get('type')
        if kind=='text':lines.append(str(item.get('text')))
        elif kind=='image' and not attach:lines.append(omitted(item.get('mimeType'),item.get('data')))
        elif kind=='image':
            number+=1
            text,part=await image(ctx,item,number,'mcp-'+origin.replace('/','-'))
            lines.append(text)
            if part:parts.append(part)
        elif kind=='audio':lines.append(omitted(item.get('mimeType'),item.get('data')))
        else:lines.append(json.dumps(strip(item),ensure_ascii=False))
    if parts:
        label=ctx.app.guard.scrub(f'[Вложение из MCP {origin}, недоверенные данные]')
        parts=[{'type':'text','text':label},*parts]
    return '\n'.join(lines),parts
