"""Входящее Rich-сообщение Telegram (Bot API 10.1+, поле Message.rich_message) → текст для модели и список медиа.

Такие посты (заголовки, таблицы, коллажи, свои эмодзи) приходят без text/caption: всё содержимое лежит
в блоках RichBlock с текстом RichText. Здесь — чтение по api.json Bot API 10.3: текст в похожем на Markdown
виде, свои эмодзи — их обычной заменой (alternative_text), медиа — пометкой на месте и отдельным списком
для скачивания. Неизвестные новые типы не роняют разбор: берётся их текст, если он есть.
"""
from __future__ import annotations

DEPTH=40
MEDIA={'photo':('photo','фото'),'video':('video','видео'),'animation':('animation','анимация'),'audio':('audio','аудио'),
       'voice_note':('voice_note','голосовое'),'document':('document','файл')}
DOWNLOAD={'voice_note':'voice'}  # вид вложения в Бендере, если он называется иначе, чем поле блока


def inline(value, depth=0) -> str:
    """RichText → строка: жирный/курсив/код в Markdown, ссылки [текст](адрес), свой эмодзи — его заменой."""
    if depth>DEPTH:return ''
    if value is None:return ''
    if isinstance(value,str):return value
    if isinstance(value,list):return ''.join(inline(item,depth+1) for item in value)
    if not isinstance(value,dict):return str(value)
    kind=value.get('type');inner=inline(value.get('text'),depth+1)
    if kind=='custom_emoji':return str(value.get('alternative_text') or '')
    if kind=='mathematical_expression':return '$'+str(value.get('expression') or '')+'$'
    if kind=='anchor':return ''
    if kind=='button':return button(value.get('button') or {},depth+1)
    if not inner.strip():return inner
    if kind=='bold':return '**'+inner+'**'
    if kind=='italic':return '_'+inner+'_'
    if kind=='strikethrough':return '~~'+inner+'~~'
    if kind=='code':return '`'+inner+'`'
    if kind=='spoiler':return '||'+inner+'||'
    if kind=='url':
        url=str(value.get('url') or '')
        return inner if not url or url==inner else '['+inner+']('+url+')'
    if kind=='text_mention':
        uid=(value.get('user') or {}).get('id')
        return inner+(f' (tg://user?id={uid})' if uid else '')
    return inner


def button(value, depth=0) -> str:
    label=inline(value.get('text'),depth+1).strip() or 'кнопка'
    target=value.get('url') or (value.get('web_app') or {}).get('url') or (value.get('login_url') or {}).get('url') or (value.get('copy_text') or {}).get('text')
    return '[кнопка «'+label+'»'+(': '+str(target) if target else '')+']'


def _caption(block, depth) -> str:
    cap=block.get('caption')
    if isinstance(cap,dict) and ('text' in cap or 'credit' in cap):
        text=inline(cap.get('text'),depth+1).strip();credit=inline(cap.get('credit'),depth+1).strip()
        return text+(' — '+credit if credit else '')
    return inline(cap,depth+1).strip()


def _quote(lines:str, credit:str='') -> str:
    body='\n'.join('> '+line if line else '>' for line in lines.split('\n'))
    return body+('\n> — '+credit if credit else '')


def _table(block, depth) -> str:
    rows=[[inline(cell.get('text'),depth+1).replace('\n',' ').replace('|','\\|').strip() for cell in row if isinstance(cell,dict)] for row in block.get('cells') or [] if isinstance(row,list)]
    rows=[row for row in rows if row]
    if not rows:return ''
    width=max(len(row) for row in rows)
    rows=[row+['']*(width-len(row)) for row in rows]
    out=['| '+' | '.join(rows[0])+' |','|'+'---|'*width]+['| '+' | '.join(row)+' |' for row in rows[1:]]
    caption=inline(block.get('caption'),depth+1).strip()
    return ((caption+'\n') if caption else '')+'\n'.join(out)


def blocks(items, depth=0) -> list[str]:
    """Блоки → абзацы текста (по строке Markdown на блок)."""
    out=[]
    if depth>DEPTH or not isinstance(items,list):return out
    for block in items:
        if not isinstance(block,dict):continue
        text=_block(block,depth)
        if text and text.strip():out.append(text.rstrip())
    return out


def _block(block, depth) -> str:
    kind=block.get('type')
    if kind in ('paragraph','footer'):return inline(block.get('text'),depth+1)
    if kind=='heading':
        size=block.get('size') if isinstance(block.get('size'),int) else 2
        return '#'*min(6,max(1,size))+' '+inline(block.get('text'),depth+1).strip()
    if kind=='pre':return '```'+str(block.get('language') or '')+'\n'+inline(block.get('text'),depth+1)+'\n```'
    if kind=='divider':return '---'
    if kind=='mathematical_expression':return '$$'+str(block.get('expression') or '')+'$$'
    if kind in ('anchor','thinking'):return ''
    if kind=='list':
        lines=[]
        for item in block.get('items') or []:
            if not isinstance(item,dict):continue
            mark=('[x] ' if item.get('is_checked') else '[ ] ') if item.get('has_checkbox') else ''
            body='\n'.join(blocks(item.get('blocks'),depth+1)).split('\n')
            label=str(item.get('label') or '•')
            lines.append(label+' '+mark+body[0]+''.join('\n   '+line for line in body[1:]))
        return '\n'.join(lines)
    if kind=='blockquote':return _quote('\n\n'.join(blocks(block.get('blocks'),depth+1)),inline(block.get('credit'),depth+1).strip())
    if kind in ('expandable_blockquote','pullquote'):return _quote(inline(block.get('text'),depth+1),inline(block.get('credit'),depth+1).strip())
    if kind in ('collage','slideshow'):
        inner=blocks(block.get('blocks'),depth+1);cap=_caption(block,depth)
        return '\n'.join(inner+([cap] if cap else []))
    if kind=='table':return _table(block,depth)
    if kind=='details':
        return '**'+inline(block.get('summary'),depth+1).strip()+'**\n'+'\n\n'.join(blocks(block.get('blocks'),depth+1))
    if kind=='map':
        where=block.get('location') or {};cap=_caption(block,depth)
        return f"[карта: {where.get('latitude')}, {where.get('longitude')}]"+(' '+cap if cap else '')
    if kind=='buttons':return ' '.join(button(item,depth+1) for item in block.get('buttons') or [] if isinstance(item,dict))
    if kind in MEDIA:
        info=block.get(MEDIA[kind][0]) or {}
        if isinstance(info,list):info=info[-1] if info else {}
        name=info.get('file_name') if isinstance(info,dict) else None
        cap=_caption(block,depth)
        return '['+MEDIA[kind][1]+(' «'+str(name)+'»' if name else '')+']'+(' '+cap if cap else '')
    # Новый тип из будущей версии API: хотя бы его текст.
    return inline(block.get('text'),depth+1) if 'text' in block else '\n'.join(blocks(block.get('blocks'),depth+1))


def text(rich) -> str:
    """Весь текст Rich-сообщения; '' — если его нет или формат не тот."""
    if not isinstance(rich,dict):return ''
    return '\n\n'.join(blocks(rich.get('blocks'))).strip()


def media(rich) -> list[tuple[str,dict]]:
    """Медиа из блоков в порядке поста: (вид вложения Бендера, объект файла Telegram)."""
    found=[]
    def walk(items, depth):
        if depth>DEPTH or not isinstance(items,list):return
        for block in items:
            if not isinstance(block,dict):continue
            kind=block.get('type')
            if kind in MEDIA:
                info=block.get(MEDIA[kind][0])
                if isinstance(info,list):info=info[-1] if info else None
                if isinstance(info,dict) and info.get('file_id'):found.append((DOWNLOAD.get(kind,kind),info))
            walk(block.get('blocks'),depth+1)
            for item in block.get('items') or []:
                if isinstance(item,dict):walk(item.get('blocks'),depth+1)
    if isinstance(rich,dict):walk(rich.get('blocks'),0)
    return found


def body(msg) -> str:
    """Текст сообщения любого вида: обычный, подпись к медиа или Rich-пост."""
    if not isinstance(msg,dict):return ''
    return msg.get('text') or msg.get('caption') or text(msg.get('rich_message'))
