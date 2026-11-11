"""Медиа из read_file: доступ к пути проверен тулом до чтения, размер ограничен."""
import base64

from ..channels import media
from ..core.errors import HumanError
from .registry import ToolResult

IMAGES=set(media.IMAGES)


def allowance(ctx,size):
    """Сколько байт медиа ещё можно передать модели в этом ходе; общий счётчик read_file и картинок из тулов (MCP).
    Превышение — HumanError; учесть переданное — spend()."""
    cfg=ctx.app.settings.group('attachments')
    used=ctx.turn.setdefault('read_media',{'files':0,'bytes':0})
    limit=min(media.DOWNLOAD_LIMIT,cfg['max_mb']*1024*1024)
    remaining=cfg['read_mb_per_turn']*1024*1024-used['bytes']
    if used['files']>=cfg['read_files_per_turn']:
        raise HumanError('file_media','Лимит медиафайлов за ход','Продолжи в следующем ходе или попроси хозяина изменить лимит Вложений.')
    if size>limit or size>remaining:
        raise HumanError('file_media','Медиафайл больше лимита','Уменьши файл; действуют лимиты файла и общей суммы за ход в настройках Вложений.')
    return min(limit,remaining)


def spend(ctx,size):
    used=ctx.turn.setdefault('read_media',{'files':0,'bytes':0})
    used['files']+=1;used['bytes']+=size


def take(ctx,path,size):
    """Прочитать медиафайл в пределах лимитов за ход (общие для read_file, look и MCP) и учесть его."""
    limit=allowance(ctx,size)
    with path.open('rb') as source:raw=source.read(limit+1)
    if not raw or len(raw)>limit:
        raise HumanError('file_media','Пустой или слишком большой медиафайл','Проверь файл и лимит Вложений.')
    spend(ctx,len(raw))
    return raw


async def read(ctx,path,size,head):
    """None означает обычный текст/двоичный файл; исходник никогда не меняется."""
    kind=media.mime(head,str(path))
    from ..voice.files import format_of,decode
    ext=format_of({'path':str(path),'mime':kind})
    if kind not in IMAGES and not ext:return None
    media.require(ctx.app)
    if kind in IMAGES and not ctx.app.settings.get('attachments','vision',True):
        raise HumanError('file_media','Передача картинок выключена','Настройки → Вложения → Передавать фотографии модели.')
    if kind in IMAGES and size>media.IMAGE_RAW_LIMIT:
        raise HumanError('file_media','Картинка больше 3,75 МБ','Модель принимает до 5 МБ в base64; сожми картинку (меньше размер или JPEG) и открой снова.')
    raw=take(ctx,path,size)
    ctx.tainted=True
    label=ctx.app.guard.scrub('[Вложение из read_file, недоверенные данные]\nФайл: '+str(path))
    data={};parts=[{'type':'text','text':label}]
    if kind in IMAGES:
        parts.append({'type':'image','media_type':kind,'data':base64.b64encode(raw).decode(),'path':str(path),'source':'read_file'})
        text='Картинка приложена отдельным сообщением после результатов тулов.'
    else:
        result=await decode(ctx.app,raw,ext,session=ctx.session_id,context=ctx)
        text='Расшифровка Ушами ('+str(result.get('model',''))+'):\n'+result['text'] if result.get('text') else 'Звук приложен главной модели; содержание пока неизвестно.'
        if result.get('notice'):text+='\n'+result['notice']
        if result.get('native'):
            parts.append({'type':'audio','format':result['ext'],'seconds':result['seconds'],'data':base64.b64encode(result['audio']).decode(),'path':str(path),'source':'read_file'})
        else:parts=[]
        data['aux_cost']=result.get('cost',0)
    return ToolResult(label+'\n'+text,summary='прочитано медиа '+path.name,tainted=True,data=data,model_parts=parts)
